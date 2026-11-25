"""管理端生图工作台。浏览器只访问本服务，不直连网关，也不能自带分组、密钥或地址。"""

from __future__ import annotations

import re
import shutil
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import or_
from sqlalchemy.orm import Session

from admin_app.db import get_db
from admin_app.deps import client_ip, require
from admin_app.errors import DomainError
from admin_app.image_relay import (
    IMAGE_CAPABILITY_NOTE,
    IMAGE_KEY_NAME,
    IMAGE_GENERATIONS_URL,
    build_generation_payload,
    combination_ready_for_live,
    ensure_member_image_key,
    ensure_model,
    ensure_model_selectable,
    ensure_verified_size_combination,
    estimate_cents,
    group_model_names,
    image_group_name,
    image_model_metadata,
    image_pricing_metadata,
    image_quality_metadata,
    image_size_metadata,
    job_directory,
    live_generation_enabled,
    normalize_quality,
    orientation_of,
    request_image_generation,
    store_live_images,
    store_mock_images,
)
from admin_app.models import ImageJob, Member
from admin_app.money import yuan
from admin_app.service import audit
from admin_app.timeutil import iso

router = APIRouter(prefix="/v1/image-workbench")

_PROMPT_MAX = 2000
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_FILE_NAME = re.compile(r"(?:mock|image)-[1-4]\.(?:svg|png|jpe?g|webp)")
_ORIENTATIONS = {"", "square", "landscape", "portrait", "auto"}
_MEDIA = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


class GenerateBody(BaseModel):
    """没有 group / key / baseURL / async。多传这些字段会被拒绝。"""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=_PROMPT_MAX)
    model: str = Field(min_length=1, max_length=80)
    n: int = Field(default=1, ge=1, le=4)
    size: str = "auto"
    quality: str = "auto"
    confirm: bool

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("请填写画面描述")
        return text

    @field_validator("confirm")
    @classmethod
    def _confirm(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("请确认后再生成")
        return value


def _own_job(db: Session, user: Member, job_id: str) -> ImageJob | None:
    if not _JOB_ID.fullmatch(job_id):
        return None
    row = db.get(ImageJob, job_id)
    if row is None or row.member_id != user.id:
        return None
    if user.tenant_id is not None and row.tenant_id != user.tenant_id:
        return None
    return row


def _image_list(row: ImageJob) -> list[str]:
    raw = row.images if isinstance(row.images, list) else []
    return [name for name in raw if isinstance(name, str)]


def _public(row: ImageJob) -> dict:
    names = _image_list(row)
    return {
        "id": row.id,
        "prompt": row.prompt,
        "model": row.model,
        "size": row.size,
        "quality": row.quality,
        "n": row.n,
        "status": row.status,
        "mock": bool(row.mock),
        "message": row.message,
        "error": row.error or "",
        "est_cost_cents": row.est_cost_cents,
        "est_cost": yuan(row.est_cost_cents),
        "orientation": orientation_of(row.size),
        "images": [
            {
                "url": f"/v1/image-workbench/jobs/{row.id}/files/{name}",
                "mock": bool(row.mock),
            }
            for name in names
        ],
        "created_at": iso(row.created_at),
    }


def _status_models(group: dict | None, ready: bool) -> list[dict]:
    if not ready or group is None:
        models = image_model_metadata(None)
        for item in models:
            item["ready"] = False
            item["selectable"] = False
        return models
    return image_model_metadata(group_model_names(group))


@router.get("/status")
def image_status(user: Member = Depends(require("image.workbench")), db: Session = Depends(get_db)) -> dict:
    reason = ""
    group = None
    try:
        group, _key = ensure_member_image_key(db, user)
    except DomainError as exc:
        reason = exc.detail
    ready = group is not None and not reason
    live = live_generation_enabled()
    return {
        "logged_in": True,
        "group_ready": ready,
        "group_reason": reason,
        "group_name": str(group.get("name") or "") if group else image_group_name(),
        "key_name": IMAGE_KEY_NAME,
        "key_ready": bool(str(user.relay_image_api_key or "").strip()),
        "mock": not live,
        "live_enabled": live,
        "models": _status_models(group, ready),
        "pricing": image_pricing_metadata(),
        "sizes": image_size_metadata(),
        "qualities": image_quality_metadata(),
        "counts": [1, 2, 3, 4],
        "capability_note": IMAGE_CAPABILITY_NOTE,
        "endpoint": {
            "method": "POST",
            "url": IMAGE_GENERATIONS_URL,
            "editable": False,
        },
        "references": {"enabled": False, "reason": "参考图与 edits 尚未实测，首版禁用"},
        "async_generation": {"enabled": False, "reason": "暂无对象存储，异步生成首版禁用，只支持同步生成"},
    }


@router.post("/generate")
def generate_image(
    body: GenerateBody,
    request: Request,
    user: Member = Depends(require("image.workbench")),
    db: Session = Depends(get_db),
) -> dict:
    quality = normalize_quality(body.quality)
    size = ensure_verified_size_combination(size=body.size, model=body.model, n=body.n, quality=quality)
    model = ensure_model(body.model)
    group, api_key = ensure_member_image_key(db, user)
    ensure_model_selectable(group, model)
    payload = build_generation_payload(model=model, prompt=body.prompt, n=body.n, size=size, quality=quality)
    job_id = uuid.uuid4().hex
    directory = job_directory(user.id, job_id)
    live = live_generation_enabled()
    try:
        if live:
            if not combination_ready_for_live(model=model, size=size, n=body.n, quality=quality):
                raise DomainError(
                    400,
                    "真实生图只接受已核验组合：gpt-image-2.5-flare、2048x1152 或 3840x2160、n=1、省略 quality。"
                    "当前组合不能发起真实调用。",
                )
            names = store_live_images(directory, request_image_generation(api_key=api_key, payload=payload, live=True))
            mock = False
            message = "已请求网关同步生图。扣费以网关实账为准。"
            action = "image.generate"
        else:
            names = store_mock_images(directory, body.n, size, body.prompt)
            mock = True
            message = "预览占位图 · 未请求真实生图接口"
            action = "image.generate_preview"
    except DomainError as exc:
        shutil.rmtree(directory, ignore_errors=True)
        if live:
            audit(
                db,
                user,
                "image.generate_failed",
                job_id,
                f"真实生图未完成：{model} {size} ×{body.n}",
                ip=client_ip(request),
            )
            db.commit()
        raise
    row = ImageJob(
        id=job_id,
        member_id=user.id,
        tenant_id=user.tenant_id,
        prompt=body.prompt,
        model=model,
        size=size,
        quality=quality,
        n=body.n,
        status="done",
        mock=mock,
        message=message,
        images=names,
        est_cost_cents=estimate_cents(body.n),
        error="",
    )
    db.add(row)
    audit(
        db,
        user,
        action,
        job_id,
        f"{'预览' if mock else '生成'} {model} {size} ×{body.n}",
        ip=client_ip(request),
    )
    db.commit()
    db.refresh(row)
    return _public(row)


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/jobs")
def list_jobs(
    query: str = "",
    orientation: str = "",
    user: Member = Depends(require("image.workbench")),
    db: Session = Depends(get_db),
) -> list[dict]:
    if orientation not in _ORIENTATIONS:
        raise DomainError(400, "不支持的方向筛选")
    text = query.strip()[:200]
    rows = db.query(ImageJob).filter(ImageJob.member_id == user.id)
    if user.tenant_id is not None:
        rows = rows.filter(ImageJob.tenant_id == user.tenant_id)
    if text:
        pattern = _like(text)
        rows = rows.filter(or_(ImageJob.prompt.ilike(pattern, escape="\\"), ImageJob.model.ilike(pattern, escape="\\")))
    found = rows.order_by(ImageJob.created_at.desc(), ImageJob.id.desc()).limit(50).all()
    if orientation:
        found = [row for row in found if orientation_of(row.size) == orientation]
    return [_public(row) for row in found]


@router.delete("/jobs/{job_id}")
def delete_job(
    job_id: str,
    request: Request,
    user: Member = Depends(require("image.workbench")),
    db: Session = Depends(get_db),
) -> dict:
    row = _own_job(db, user, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    db.delete(row)
    audit(db, user, "image.delete", job_id, "删除一条生图记录", ip=client_ip(request))
    db.commit()
    shutil.rmtree(job_directory(user.id, job_id), ignore_errors=True)
    return {"ok": True}


@router.get("/jobs/{job_id}/files/{name}")
def job_file(
    job_id: str,
    name: str,
    download: bool = False,
    user: Member = Depends(require("image.workbench")),
    db: Session = Depends(get_db),
) -> FileResponse:
    row = _own_job(db, user, job_id)
    if row is None or not _FILE_NAME.fullmatch(name) or name not in _image_list(row):
        raise HTTPException(status_code=404, detail="文件不存在")
    directory = job_directory(user.id, job_id).resolve()
    path = (directory / name).resolve()
    if path.parent != directory or not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    suffix = path.suffix.lower()
    return FileResponse(
        path,
        media_type=_MEDIA.get(suffix, "application/octet-stream"),
        filename=name,
        content_disposition_type="attachment" if download else "inline",
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/references")
def references_disabled(user: Member = Depends(require("image.workbench"))) -> dict:
    raise DomainError(403, "参考图与 edits 尚未实测，首版禁用")
