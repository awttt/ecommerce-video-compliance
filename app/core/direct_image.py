"""生图直接请求 token4u.cc 主站，不经过 admin.token4u.cc。"""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import re
import shutil
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, Response

from app.core.relay import KEY_NAME as REVIEW_KEY_NAME
from app.core.relay import RelayClient, RelayError
from app.paths import data_dir

log = logging.getLogger(__name__)

IMAGE_KEY_NAME = "生图专用"
GENERATIONS_URL = "https://token4u.cc/v1/images/generations"
EDITS_URL = "https://token4u.cc/v1/images/edits"
_ALLOWED_SIZES = {"auto", "1024x1024", "1024x1536", "1536x1024", "2048x1152", "3840x2160"}
_ALLOWED_QUALITIES = {"auto", "low", "medium", "high"}
_REFERENCE_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}
_MAX_REFERENCES = 4
_MAX_REFERENCE_BYTES = 4 * 1024 * 1024
_MODELS = (
    "gpt-image-2.5-flare",
    "gpt-image-2.5-sunburst",
    "gpt-image-2.6-flare",
    "gpt-image-2.6-sunburst",
)
_SIZES = ("auto", "1024x1024", "1024x1536", "1536x1024", "2048x1152", "3840x2160")
_JOB_LIMIT = 50
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_FILE_NAME = re.compile(r"^image-[1-4]\.(?:png|jpe?g|webp)$")
_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def image_key_from_items(items: list[dict]) -> tuple[str, dict]:
    """只取名称完全是「生图专用」的密钥。审核钥即使同名分组也不用。"""
    matched = [
        item
        for item in items
        if str(item.get("name") or "").strip() == IMAGE_KEY_NAME
        and str(item.get("name") or "").strip() != REVIEW_KEY_NAME
    ]
    if not matched:
        raise LookupError(f"当前账号没有密钥「{IMAGE_KEY_NAME}」。不会改用审核密钥。")
    if len(matched) > 1:
        raise LookupError(f"密钥列表里有多个「{IMAGE_KEY_NAME}」，无法确定使用哪一个。")
    item = matched[0]
    secret = ""
    for field in ("key", "api_key", "secret"):
        secret = str(item.get(field) or "").strip()
        if secret:
            break
    if not secret:
        raise LookupError(f"密钥「{IMAGE_KEY_NAME}」已存在，但列表没有返回可用内容。不会改用审核密钥。")
    return secret, item


def _status_payload(*, ready: bool, reason: str, group_name: str) -> dict:
    return {
        "logged_in": True,
        "group_ready": ready,
        "group_reason": reason,
        "group_name": group_name,
        "key_name": IMAGE_KEY_NAME,
        "key_ready": ready,
        "mock": False,
        "live_enabled": ready,
        "models": [
            {"id": model, "label": model, "ready": True, "selectable": True, "upstream_verified": False}
            for model in _MODELS
        ],
        "pricing": {
            "currency": "CNY",
            "unit": "张",
            "price_per_image": 0,
            "display": "按网关实账",
            "note": "费用以 token4u 实账为准，这里不预估。",
        },
        "sizes": [
            {"value": size, "label": "自动" if size == "auto" else size, "verified": False, "enabled": True}
            for size in _SIZES
        ],
        "qualities": [
            {"value": "auto", "label": "自动", "enabled": True},
            {"value": "high", "label": "高", "enabled": True},
            {"value": "medium", "label": "标准", "enabled": True},
            {"value": "low", "label": "低", "enabled": True},
        ],
        "counts": [1, 2, 3, 4],
        "endpoint": {"method": "POST", "url": GENERATIONS_URL, "editable": False},
        "references": {"enabled": True, "reason": "最多 4 张，仅 PNG、JPEG、WebP，单张不超过 4MB。"},
        "async_generation": {"enabled": False, "reason": "只支持同步生成"},
    }


def _group_name(item: dict) -> str:
    raw = item.get("group") if isinstance(item.get("group"), dict) else {}
    return str(item.get("group_name") or raw.get("name") or IMAGE_KEY_NAME)


async def _listed_models(secret: str) -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0), trust_env=True) as client:
            response = await client.get(
                "https://token4u.cc/v1/models",
                headers={"Authorization": f"Bearer {secret}", "Accept": "application/json"},
            )
        data = response.json().get("data") if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return list(_MODELS)
    found = [
        str(item.get("id"))
        for item in data or []
        if isinstance(item, dict) and "image" in str(item.get("id") or "").lower()
    ]
    return found or list(_MODELS)


async def _account_key() -> tuple[str, dict]:
    from app.services.manager import get_manager

    access, _refresh = get_manager().relay_credentials()
    if not access:
        raise PermissionError("请先登录后再生图")
    items = await RelayClient().list_keys(access)
    return image_key_from_items(items)


def parse_reference(value: str) -> tuple[str, bytes, str]:
    """把 data URL 还原成上传文件。只接受 PNG、JPEG、WebP。"""
    text = str(value or "").strip()
    if not text.startswith("data:image/") or "," not in text:
        raise ValueError("参考图必须是 PNG、JPEG 或 WebP")
    header, encoded = text.split(",", 1)
    mime = header[5:].split(";", 1)[0].strip().lower()
    ext = _REFERENCE_TYPES.get(mime)
    if ext is None or ";base64" not in header.lower():
        raise ValueError("参考图必须是 PNG、JPEG 或 WebP")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise ValueError("参考图无法解析") from None
    if not raw or len(raw) > _MAX_REFERENCE_BYTES:
        raise ValueError("单张参考图不能超过 4MB")
    return ext, raw, mime


def upstream_request(body: dict) -> tuple[str, dict | None, list[tuple] | None]:
    """没有参考图走生成接口；有参考图走编辑接口。尺寸和质量都会带上。"""
    prompt = str(body.get("prompt") or "").strip()
    model = str(body.get("model") or "").strip()
    size = str(body.get("size") or "auto").strip() or "auto"
    quality = str(body.get("quality") or "auto").strip() or "auto"
    count = int(body.get("n") or 1)
    if size not in _ALLOWED_SIZES:
        raise ValueError("尺寸不在可选范围内")
    if quality not in _ALLOWED_QUALITIES:
        raise ValueError("质量只支持自动、低、标准、高")
    references = body.get("references") or []
    if references is None:
        references = []
    if not isinstance(references, list):
        raise ValueError("参考图格式不正确")
    if len(references) > _MAX_REFERENCES:
        raise ValueError("参考图最多 4 张")
    fields = {"model": model, "prompt": prompt, "n": str(count)}
    if size != "auto":
        fields["size"] = size
    if quality != "auto":
        fields["quality"] = quality
    if not references:
        payload = {"model": model, "prompt": prompt, "n": count}
        if size != "auto":
            payload["size"] = size
        if quality != "auto":
            payload["quality"] = quality
        return GENERATIONS_URL, payload, None
    files: list[tuple] = []
    for index, item in enumerate(references, start=1):
        ext, raw, mime = parse_reference(str(item))
        files.append(("image" if len(references) == 1 else "image[]", (f"reference-{index}.{ext}", raw, mime)))
    return EDITS_URL, fields, files


def _upstream_message(status: int, body: object) -> str:
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if body.get("message"):
            return str(body["message"])
    return f"生图请求被拒绝（{status}）"


def image_jobs_root() -> Path:
    path = data_dir() / "image_jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_jobs() -> list[dict]:
    """从数据目录读生成记录。重启后仍在，不依赖进程内存。"""
    found: list[dict] = []
    root = image_jobs_root()
    for child in root.iterdir():
        meta = child / "job.json"
        if not meta.is_file():
            continue
        try:
            job = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(job, dict) and isinstance(job.get("id"), str):
            found.append(job)
    found.sort(key=lambda job: str(job.get("created_at") or ""), reverse=True)
    return found


def remove_job(job_id: str) -> None:
    if not _JOB_ID.fullmatch(job_id):
        return
    root = image_jobs_root().resolve()
    directory = (root / job_id).resolve()
    if directory.parent != root:
        return
    shutil.rmtree(directory, ignore_errors=True)


def write_job(job: dict) -> None:
    job_id = str(job.get("id") or "")
    if not _JOB_ID.fullmatch(job_id):
        raise ValueError("任务编号无效")
    directory = image_jobs_root() / job_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    stale = load_jobs()[_JOB_LIMIT:]
    for item in stale:
        remove_job(str(item.get("id") or ""))


def read_saved_file(job_id: str, name: str) -> tuple[bytes, str] | None:
    if not _JOB_ID.fullmatch(job_id) or not _FILE_NAME.fullmatch(name):
        return None
    root = image_jobs_root().resolve()
    directory = (root / job_id).resolve()
    path = (directory / name).resolve()
    if directory.parent != root or path.parent != directory or not path.is_file():
        return None
    return path.read_bytes(), _MEDIA.get(path.suffix.lower(), "application/octet-stream")


def _sniff_suffix(payload: bytes) -> str:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if payload.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if payload.startswith(b"RIFF") and len(payload) >= 12 and payload[8:12] == b"WEBP":
        return ".webp"
    return ""


def _host_is_blocked(host: str) -> bool:
    lowered = host.strip().lower().rstrip(".")
    if not lowered or lowered in {"localhost", "metadata.google.internal"} or lowered.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return any((ip.is_private, ip.is_loopback, ip.is_link_local, ip.is_reserved, ip.is_multicast, ip.is_unspecified))


def _download_public_image(url: str) -> bytes | None:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        return None
    if _host_is_blocked(host):
        return None
    try:
        infos = socket.getaddrinfo(host, 443)
    except socket.gaierror:
        return None
    if any(_host_is_blocked(info[4][0]) for info in infos):
        return None
    try:
        with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), trust_env=True, follow_redirects=False) as client:
            response = client.get(url)
    except httpx.HTTPError:
        return None
    if response.status_code >= 300 or len(response.content) > _MAX_IMAGE_BYTES:
        return None
    return response.content


def store_result_images(job_id: str, urls: list[str]) -> list[dict]:
    """把图片字节写到数据目录。临时网关地址下载失败时，仍保留原地址。"""
    directory = image_jobs_root() / job_id
    directory.mkdir(parents=True, exist_ok=True)
    images: list[dict] = []
    for index, url in enumerate(urls, start=1):
        payload = b""
        if url.startswith("data:image"):
            try:
                payload = base64.b64decode(url.split(",", 1)[1], validate=False)
            except (ValueError, IndexError):
                payload = b""
        else:
            downloaded = _download_public_image(url)
            payload = downloaded or b""
        suffix = _sniff_suffix(payload) if payload and len(payload) <= _MAX_IMAGE_BYTES else ""
        if not suffix:
            images.append({"url": url, "mock": False})
            continue
        name = f"image-{index}{suffix}"
        (directory / name).write_bytes(payload)
        images.append({"url": f"/v1/image-workbench/jobs/{job_id}/files/{name}", "mock": False})
    return images


def _image_urls(body: object) -> list[str]:
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return []
    urls: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if url:
            urls.append(url)
            continue
        encoded = str(item.get("b64_json") or "").strip()
        if encoded:
            urls.append(f"data:image/png;base64,{encoded}")
    return urls


async def handle_image_workbench(path: str, request: Request) -> Response:
    """控制台自己处理生图。其余 /v1 仍转给线上管理后台。"""
    route = path.strip("/")
    try:
        if request.method == "GET" and route == "status":
            try:
                _secret, item = await _account_key()
            except PermissionError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=401)
            except LookupError as exc:
                return JSONResponse(_status_payload(ready=False, reason=str(exc), group_name=""), status_code=200)
            except RelayError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=exc.status if exc.status else 502)
            payload = _status_payload(ready=True, reason="", group_name=_group_name(item))
            payload["models"] = [
                {"id": model, "label": model, "ready": True, "selectable": True, "upstream_verified": False}
                for model in await _listed_models(_secret)
            ]
            return JSONResponse(payload)
        if request.method == "GET" and route.startswith("jobs/") and "/files/" in route:
            job_id, _, name = route.removeprefix("jobs/").partition("/files/")
            found = read_saved_file(job_id, name)
            if found is None:
                return JSONResponse({"detail": "文件不存在"}, status_code=404)
            payload, media = found
            download = str(request.query_params.get("download") or "") in {"1", "true"}
            disposition = "attachment" if download else "inline"
            return Response(
                content=payload,
                media_type=media,
                headers={
                    "Cache-Control": "private, no-store",
                    "Content-Disposition": f'{disposition}; filename="{name}"',
                },
            )
        if request.method == "GET" and route == "jobs":
            query = str(request.query_params.get("query") or "").strip().lower()
            orientation = str(request.query_params.get("orientation") or "").strip()
            rows = load_jobs()
            if query:
                rows = [job for job in rows if query in job["prompt"].lower() or query in job["model"].lower()]
            if orientation:
                rows = [job for job in rows if job["orientation"] == orientation]
            return JSONResponse(rows)
        if request.method == "DELETE" and route.startswith("jobs/") and "/files/" not in route:
            remove_job(route.removeprefix("jobs/").strip("/"))
            return JSONResponse({"ok": True})
        if request.method == "POST" and route == "generate":
            return await _generate(request)
    except Exception:
        log.exception("生图接口处理失败")
        return JSONResponse({"detail": "生图接口处理失败"}, status_code=500)
    return JSONResponse({"detail": "没有这个接口"}, status_code=404)


async def _generate(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"detail": "请求格式不正确"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"detail": "请求格式不正确"}, status_code=400)
    prompt = str(body.get("prompt") or "").strip()
    model = str(body.get("model") or "").strip()
    size = str(body.get("size") or "auto").strip() or "auto"
    quality = str(body.get("quality") or "auto").strip() or "auto"
    try:
        count = int(body.get("n") or 1)
    except (TypeError, ValueError):
        count = 0
    if not prompt:
        return JSONResponse({"detail": "请填写画面描述"}, status_code=400)
    if not model:
        return JSONResponse({"detail": "请选择生图模型"}, status_code=400)
    if count < 1 or count > 4:
        return JSONResponse({"detail": "张数只支持 1 到 4"}, status_code=400)
    try:
        url, payload, files = upstream_request(body)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    try:
        secret, _item = await _account_key()
    except PermissionError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    except LookupError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=403)
    except RelayError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status if exc.status else 502)
    headers = {
        "Authorization": f"Bearer {secret}",
        "Accept": "application/json",
        "User-Agent": "ecommerce-video-compliance",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0), trust_env=True) as client:
            if files is None:
                headers["Content-Type"] = "application/json"
                upstream = await client.post(url, json=payload, headers=headers)
            else:
                upstream = await client.post(url, data=payload, files=files, headers=headers)
    except httpx.TimeoutException:
        return JSONResponse({"detail": "生图请求超时。不能确认网关是否已经扣费，请先核对账单。"}, status_code=504)
    except httpx.HTTPError:
        return JSONResponse({"detail": "连不上 token4u.cc。这次没有确认请求已发出。"}, status_code=502)
    try:
        upstream_body = upstream.json()
    except ValueError:
        upstream_body = {}
    if upstream.status_code >= 400:
        return JSONResponse({"detail": _upstream_message(upstream.status_code, upstream_body)}, status_code=upstream.status_code)
    urls = _image_urls(upstream_body)
    if not urls:
        return JSONResponse({"detail": "生图服务没有返回图片"}, status_code=502)
    for url in urls:
        if url.startswith("data:image"):
            try:
                base64.b64decode(url.split(",", 1)[1], validate=False)
            except (ValueError, IndexError):
                return JSONResponse({"detail": "生图服务返回的图片无法解析"}, status_code=502)
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "prompt": prompt,
        "model": model,
        "size": size,
        "quality": quality,
        "n": count,
        "status": "succeeded",
        "mock": False,
        "message": "已由 token4u.cc 返回",
        "error": "",
        "est_cost_cents": 0,
        "est_cost": 0,
        "orientation": _orientation(size),
        "images": store_result_images(job_id, urls),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    write_job(job)
    return JSONResponse(job)


def _orientation(size: str) -> str:
    text = size.lower()
    if "x" not in text:
        return "auto"
    width_text, _, height_text = text.partition("x")
    try:
        width, height = int(width_text), int(height_text)
    except ValueError:
        return "auto"
    if width == height:
        return "square"
    return "landscape" if width > height else "portrait"
