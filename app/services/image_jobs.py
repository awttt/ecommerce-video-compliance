"""本机生图记录。文件按批次编号存放，查询和下载都核对登录账号。"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import httpx

from app.core.image_contract import (
    IMAGE_KEY_NAME,
    MAX_BATCHES,
    MAX_REFERENCE_BYTES,
    MAX_REFERENCES,
    NOTICE,
    PREVIEW_MODEL,
    PREVIEW_MODEL_LABEL,
    SIZES,
    ImageError,
    LocalRequest,
    current_contract,
    current_transport,
    gateway_base,
    group_label,
    mock_delay,
    orientation_of,
    prepare_upstream,
    preview_png,
    provider_mode,
    resolve_image_group,
    size_label,
    sniff_image,
)
from app.core.relay import USER_AGENT, RelayError
from app.logging_setup import redact
from app.paths import data_dir, is_inside

log = logging.getLogger(__name__)

_ID = re.compile(r"^[0-9a-f]{32}$")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    prompt TEXT NOT NULL,
    model TEXT NOT NULL,
    size TEXT NOT NULL,
    orientation TEXT NOT NULL,
    count INTEGER NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    preview INTEGER NOT NULL,
    upstream_sent INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_image_owner ON batches(owner, created_at);
CREATE TABLE IF NOT EXISTS files (
    batch_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    idx INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    PRIMARY KEY (batch_id, kind, idx)
);
"""

_guard = asyncio.Lock()
_inflight: set[str] = set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _owner_key(email: str) -> str:
    return email.strip().lower()


def _root() -> Path:
    path = (data_dir() / "image-workbench").resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def batch_directory(batch_id: str) -> Path:
    if not _ID.fullmatch(batch_id):
        raise ImageError("记录不存在", 404)
    root = _root()
    path = (root / batch_id).resolve()
    if not is_inside(path, root) or path.parent != root:
        raise ImageError("记录不存在", 404)
    return path


@asynccontextmanager
async def _db():
    connection = await aiosqlite.connect(data_dir() / "image_workbench.db")
    connection.row_factory = aiosqlite.Row
    try:
        await connection.executescript(_SCHEMA)
        yield connection
    finally:
        await connection.close()


def reset_image_jobs() -> None:
    _inflight.clear()


async def shutdown_image_jobs() -> None:
    reset_image_jobs()


async def sweep_interrupted() -> None:
    async with _db() as db:
        await db.execute(
            "UPDATE batches SET status = 'failed', message = ?, error = ? WHERE status = 'running'",
            ("程序退出时生成未完成", "interrupted"),
        )
        await db.commit()


def _manager():
    from app.services.manager import get_manager

    return get_manager()


def current_owner() -> str:
    manager = _manager()
    manager.ensure_logged_in()
    return _owner_key(manager._account_email())


async def describe() -> dict:
    mode = provider_mode()
    if mode == "mock":
        return _status(True, "预览模拟，不是真实上游生图，不会请求上游，也不会扣费。")
    if mode != "token4u":
        return _status(False, "生图尚未配置，功能已禁用。不会改用审核分组，也不会直连上游。")
    try:
        configured = group_label() != "未配置"
    except ImageError as exc:
        return _status(False, exc.message)
    if not configured:
        return _status(False, "生图专用分组尚未配置，功能已禁用。不会改用审核分组，也不会直连上游。")
    try:
        groups = await _manager().list_image_groups()
        resolve_image_group(groups)
    except ImageError as exc:
        return _status(False, exc.message)
    except RelayError:
        return _status(False, "暂时无法确认生图分组，已拒绝。不会改用审核分组。")
    contract = current_contract()
    if not contract.confirmed or contract.parser is None or not contract.models:
        return _status(False, "生图专用分组已按服务端配置核对，但上游契约尚未确认，生成已禁用。")
    return _status(
        True,
        "将使用当前登录账号在专用分组中的密钥。尺寸、张数、画质、参考图和异步开关不会发给上游。",
    )


def _status(enabled: bool, reason: str) -> dict:
    mode = provider_mode()
    preview = mode == "mock"
    contract = current_contract()
    if preview:
        models = [{"id": PREVIEW_MODEL, "label": PREVIEW_MODEL_LABEL}]
    elif enabled:
        models = [{"id": model, "label": model} for model in contract.models]
    else:
        models = []
    try:
        label = group_label()
    except ImageError:
        label = "未配置"
    return {
        "enabled": enabled,
        "mode": mode if mode in {"mock", "token4u"} else "unconfigured",
        "preview": preview,
        "reason": reason,
        "group_label": label,
        "models": models,
        "sizes": [
            {"id": item_id, "label": label_text, "orientation": orientation}
            for item_id, label_text, orientation in SIZES
        ],
        "counts": [1, 2, 3, 4],
        "quality": "auto",
        "quality_enabled": False,
        "async_enabled": False,
        "reference_local": preview,
        "upstream_fields": sorted(contract.fields) if contract.confirmed else [],
        "notice": NOTICE,
    }


async def generate(local: LocalRequest, references: list[tuple[str, bytes]]) -> dict:
    mode = provider_mode()
    if mode == "unconfigured":
        raise ImageError("生图尚未配置，功能已禁用。不会改用审核分组，也不会直连上游。", 409)
    owner = current_owner()
    checked = _checked_references(references)
    if mode != "mock" and checked:
        raise ImageError("参考图还不能发给上游，已取消这次生成。", 400)
    async with _guard:
        if owner in _inflight:
            raise ImageError("已有生成任务进行中，请稍后再试。", 409)
        _inflight.add(owner)
    try:
        if mode == "mock":
            return await _run_mock(owner, local, checked)
        return await _run_token4u(owner, local)
    finally:
        _inflight.discard(owner)


def _checked_references(references: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    if len(references) > MAX_REFERENCES:
        raise ImageError(f"参考图最多 {MAX_REFERENCES} 张", 400)
    checked: list[tuple[str, bytes]] = []
    for media_type, blob in references:
        sniffed = sniff_image(blob)
        if media_type not in {"image/png", "image/jpeg", "image/webp"} or sniffed != media_type:
            raise ImageError("参考图只接受 PNG、JPEG 或 WebP", 415)
        if len(blob) > MAX_REFERENCE_BYTES:
            raise ImageError("参考图超过 4MB 限制", 413)
        checked.append((sniffed, blob))
    return checked


async def _run_mock(owner: str, local: LocalRequest, references: list[tuple[str, bytes]]) -> dict:
    batch_id = uuid.uuid4().hex
    await _insert(
        batch_id,
        owner,
        local,
        status="running",
        message="正在生成本地预览…",
        preview=True,
        upstream_sent=False,
    )
    delay = mock_delay()
    if delay:
        await asyncio.sleep(delay)
    folder = batch_directory(batch_id)
    folder.mkdir(parents=True, exist_ok=True)
    images = [(preview_png(local.size, index), "image/png") for index in range(local.count)]
    await _write_files(batch_id, folder, images, references)
    await _finish(batch_id, status="done", message="预览模拟完成。这不是真实上游生图，也没有扣费。", error="")
    await _prune(owner)
    return await read_batch(batch_id, owner)


async def _run_token4u(owner: str, local: LocalRequest) -> dict:
    contract = current_contract()
    if not contract.confirmed or contract.parser is None:
        raise ImageError("生图上游契约尚未确认，已拒绝调用。", 409)
    if not contract.fields or not contract.fields <= {"model", "prompt"}:
        raise ImageError("契约包含尚未确认的参数，已拒绝调用。", 409)
    try:
        gateway_base()
        groups = await _manager().list_image_groups()
        group_id = resolve_image_group(groups)
        api_key = await _manager().ensure_image_key(group_id)
        prepared = prepare_upstream(local, api_key, contract)
    except RelayError as exc:
        log.warning("image key was not issued: %s", redact(str(exc)))
        raise ImageError("没能准备生图密钥，审核密钥未改动。", 502) from None
    batch_id = uuid.uuid4().hex
    await _insert(
        batch_id,
        owner,
        local,
        status="running",
        message="正在请求专用分组…",
        preview=False,
        upstream_sent=False,
    )
    try:
        raw = await post_upstream(prepared.url, prepared.body, prepared.api_key)
        try:
            images = contract.parser(raw)
        except Exception:
            log.warning("image result could not be read")
            raise ImageError("生图结果无法读取。", 502) from None
        if (
            not images
            or len(images) > 4
            or any(not isinstance(item, bytes) or not item.startswith(b"\x89PNG\r\n\x1a\n") for item in images)
        ):
            raise ImageError("生图结果无法读取。", 502)
    except ImageError as exc:
        await _finish(batch_id, status="failed", message=exc.message, error="upstream")
        raise
    folder = batch_directory(batch_id)
    folder.mkdir(parents=True, exist_ok=True)
    await _write_files(batch_id, folder, [(item, "image/png") for item in images], [])
    await _finish(batch_id, status="done", message="生成完成。", error="", upstream_sent=True)
    await _prune(owner)
    return await read_batch(batch_id, owner)


async def post_upstream(url: str, body: dict, api_key: str) -> bytes:
    if not url.startswith("https://token4u.cc/v1/"):
        raise ImageError("生图网关只能是 token4u，已拒绝其他地址。", 409)
    timeout = httpx.Timeout(90.0, connect=10.0)
    try:
        async with httpx.AsyncClient(
            transport=current_transport(),
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )
    except httpx.TimeoutException as exc:
        log.warning("image upstream timeout: %s", type(exc).__name__)
        raise ImageError("生图请求超时，请稍后再试。", 504) from None
    except httpx.HTTPError as exc:
        log.warning("image upstream connection failed: %s", type(exc).__name__)
        raise ImageError("连不上生图网关，请检查网络。", 502) from None
    if response.is_redirect or response.status_code >= 300:
        log.warning("image upstream rejected status=%s", response.status_code)
        raise ImageError("生图网关拒绝了这次请求。", 502)
    return response.content


async def list_batches(owner: str, query: str, orientation: str) -> list[dict]:
    needle = query.strip()
    if len(needle) > 200:
        raise ImageError("搜索词太长", 400)
    if orientation not in {"", "all", "square", "landscape", "portrait", "auto"}:
        raise ImageError("不支持这个方向", 400)
    like = f"%{_escape_like(needle)}%" if needle else ""
    async with _db() as db:
        cursor = await db.execute(
            """
            SELECT * FROM batches
            WHERE owner = ?
              AND (? = '' OR prompt LIKE ? ESCAPE '\\' OR model LIKE ? ESCAPE '\\')
              AND (? IN ('', 'all') OR orientation = ?)
            ORDER BY created_at DESC
            """,
            (owner, needle, like, like, orientation, orientation),
        )
        rows = await cursor.fetchall()
        listed = [dict(row) for row in rows]
    return [await _public(row) for row in listed]


async def read_batch(batch_id: str, owner: str) -> dict:
    row = await _owned(batch_id, owner)
    return await _public(row)


async def delete_batch(batch_id: str, owner: str) -> None:
    await _owned(batch_id, owner)
    async with _db() as db:
        await db.execute("DELETE FROM files WHERE batch_id = ?", (batch_id,))
        await db.execute("DELETE FROM batches WHERE id = ? AND owner = ?", (batch_id, owner))
        await db.commit()
    folder = batch_directory(batch_id)
    if folder.is_dir():
        for child in folder.iterdir():
            if child.is_file():
                child.unlink()
        folder.rmdir()


async def open_file(batch_id: str, owner: str, kind: str, index: int) -> tuple[Path, str, str]:
    if kind not in {"result", "reference"} or index < 0 or index >= 4:
        raise ImageError("记录不存在", 404)
    await _owned(batch_id, owner)
    async with _db() as db:
        cursor = await db.execute(
            "SELECT media_type FROM files WHERE batch_id = ? AND kind = ? AND idx = ?",
            (batch_id, kind, index),
        )
        row = await cursor.fetchone()
    if row is None:
        raise ImageError("记录不存在", 404)
    folder = batch_directory(batch_id)
    path = (folder / f"{kind}-{index}.bin").resolve()
    if not is_inside(path, folder) or not path.is_file():
        raise ImageError("记录不存在", 404)
    filename = f"yujing-preview-{index + 1}.png" if kind == "result" else f"yujing-reference-{index + 1}"
    return path, str(row["media_type"]), filename


async def _owned(batch_id: str, owner: str) -> dict:
    if not _ID.fullmatch(batch_id):
        raise ImageError("记录不存在", 404)
    async with _db() as db:
        cursor = await db.execute("SELECT * FROM batches WHERE id = ? AND owner = ?", (batch_id, owner))
        row = await cursor.fetchone()
    if row is None:
        raise ImageError("记录不存在", 404)
    return dict(row)


async def _insert(
    batch_id: str,
    owner: str,
    local: LocalRequest,
    *,
    status: str,
    message: str,
    preview: bool,
    upstream_sent: bool,
) -> None:
    created = _now()
    async with _db() as db:
        await db.execute(
            """
            INSERT INTO batches (
                id, owner, prompt, model, size, orientation, count, status, message,
                preview, upstream_sent, error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)
            """,
            (
                batch_id,
                owner,
                local.prompt,
                local.model,
                local.size,
                orientation_of(local.size),
                local.count,
                status,
                message,
                1 if preview else 0,
                1 if upstream_sent else 0,
                created,
                created,
            ),
        )
        await db.commit()


async def _finish(batch_id: str, *, status: str, message: str, error: str, upstream_sent: bool = False) -> None:
    async with _db() as db:
        await db.execute(
            """
            UPDATE batches
            SET status = ?, message = ?, error = ?, upstream_sent = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, message, error, 1 if upstream_sent else 0, _now(), batch_id),
        )
        await db.commit()


async def _write_files(
    batch_id: str,
    folder: Path,
    images: list[tuple[bytes, str]],
    references: list[tuple[str, bytes]],
) -> None:
    async with _db() as db:
        for index, (blob, media_type) in enumerate(images):
            (folder / f"result-{index}.bin").write_bytes(blob)
            await db.execute(
                "INSERT INTO files (batch_id, kind, idx, media_type) VALUES (?, 'result', ?, ?)",
                (batch_id, index, media_type),
            )
        for index, (media_type, blob) in enumerate(references):
            (folder / f"reference-{index}.bin").write_bytes(blob)
            await db.execute(
                "INSERT INTO files (batch_id, kind, idx, media_type) VALUES (?, 'reference', ?, ?)",
                (batch_id, index, media_type),
            )
        await db.commit()


async def _prune(owner: str) -> None:
    async with _db() as db:
        cursor = await db.execute(
            """
            SELECT id FROM batches
            WHERE owner = ? AND status != 'running'
            ORDER BY created_at DESC
            LIMIT -1 OFFSET ?
            """,
            (owner, MAX_BATCHES),
        )
        rows = await cursor.fetchall()
        stale = [str(row["id"]) for row in rows]
    for batch_id in stale:
        await delete_batch(batch_id, owner)


async def _public(row: dict) -> dict:
    async with _db() as db:
        cursor = await db.execute(
            "SELECT kind, idx FROM files WHERE batch_id = ? ORDER BY kind, idx",
            (row["id"],),
        )
        files = await cursor.fetchall()
    results = [int(item["idx"]) for item in files if item["kind"] == "result"]
    references = [int(item["idx"]) for item in files if item["kind"] == "reference"]
    model = str(row["model"])
    return {
        "id": row["id"],
        "prompt": row["prompt"],
        "model": model,
        "model_label": PREVIEW_MODEL_LABEL if model == PREVIEW_MODEL else model,
        "size": row["size"],
        "size_label": size_label(str(row["size"])),
        "orientation": row["orientation"],
        "count": row["count"],
        "status": row["status"],
        "message": row["message"],
        "preview": bool(row["preview"]),
        "upstream_sent": bool(row["upstream_sent"]),
        "created_at": row["created_at"],
        "files": [
            {"index": index, "url": f"/api/images/batches/{row['id']}/files/{index}"}
            for index in results
        ],
        "references": [
            {"index": index, "url": f"/api/images/batches/{row['id']}/references/{index}"}
            for index in references
        ],
    }


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def image_key_name() -> str:
    return IMAGE_KEY_NAME
