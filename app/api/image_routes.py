"""桌面工具里的生图接口。分组、网关和密钥只由服务端决定。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from app.api.routes import require_login
from app.core.image_contract import (
    FORBIDDEN_CLIENT_FIELDS,
    MAX_REFERENCE_BYTES,
    ImageError,
    parse_request,
    provider_mode,
    sniff_image,
)
from app.services.image_jobs import (
    current_owner,
    delete_batch,
    describe,
    generate,
    list_batches,
    open_file,
    read_batch,
)

router = APIRouter(prefix="/api/images", dependencies=[Depends(require_login)])


def _http(exc: ImageError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.message)


@router.get("/status")
async def image_status() -> dict:
    try:
        return await describe()
    except ImageError as exc:
        raise _http(exc) from exc


@router.post("/generations")
async def image_generate(request: Request) -> dict:
    try:
        payload, references = await _read_request(request)
        mode = provider_mode()
        if mode == "unconfigured":
            raise ImageError("生图尚未配置，功能已禁用。不会改用审核分组，也不会直连上游。", 409)
        local = parse_request(payload, mode=mode)
        return await generate(local, references)
    except ImageError as exc:
        raise _http(exc) from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="请求格式不正确") from exc


@router.get("/batches")
async def image_batches(q: str = "", orientation: str = "all") -> list:
    try:
        return await list_batches(current_owner(), q, orientation)
    except ImageError as exc:
        raise _http(exc) from exc


@router.get("/batches/{batch_id}")
async def image_batch(batch_id: str) -> dict:
    try:
        return await read_batch(batch_id, current_owner())
    except ImageError as exc:
        raise _http(exc) from exc


@router.delete("/batches/{batch_id}")
async def image_delete(batch_id: str) -> dict:
    try:
        await delete_batch(batch_id, current_owner())
    except ImageError as exc:
        raise _http(exc) from exc
    return {"ok": True}


@router.get("/batches/{batch_id}/files/{index}")
async def image_file(batch_id: str, index: int, download: int = 0):
    return await _file(batch_id, "result", index, download)


@router.get("/batches/{batch_id}/references/{index}")
async def image_reference(batch_id: str, index: int, download: int = 0):
    return await _file(batch_id, "reference", index, download)


async def _file(batch_id: str, kind: str, index: int, download: int):
    try:
        path, media, filename = await open_file(batch_id, current_owner(), kind, index)
    except ImageError as exc:
        raise _http(exc) from exc
    disposition = "attachment" if download else "inline"
    return FileResponse(
        path,
        media_type=media,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'{disposition}; filename="{filename}"',
        },
    )


async def _read_request(request: Request) -> tuple[dict, list[tuple[str, bytes]]]:
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        form = await request.form()
        payload: dict = {}
        references: list[tuple[str, bytes]] = []
        for key, value in form.multi_items():
            name = str(key)
            if name.lower() in FORBIDDEN_CLIENT_FIELDS:
                raise ImageError("不能指定分组、网关或密钥", 400)
            if hasattr(value, "read"):
                if name != "references":
                    raise ImageError("不能指定分组、网关或密钥", 400)
                blob = await _read_limited(value)
                media = sniff_image(blob)
                if media is None:
                    raise ImageError("参考图只接受 PNG、JPEG 或 WebP", 415)
                references.append((media, blob))
                continue
            payload[name] = value
        return payload, references
    raw = await request.body()
    if not raw:
        raise ImageError("请先填写提示词", 400)
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise ImageError("请求格式不正确", 400)
    from app.core.image_contract import reject_client_overrides

    reject_client_overrides(body)
    if body.get("references") or body.get("reference_images"):
        raise ImageError("参考图请作为文件上传", 400)
    return body, []


async def _read_limited(upload) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        block = await upload.read(64 * 1024)
        if not block:
            break
        size += len(block)
        if size > MAX_REFERENCE_BYTES:
            raise ImageError("参考图超过 4MB 限制", 413)
        chunks.append(block)
    return b"".join(chunks)
