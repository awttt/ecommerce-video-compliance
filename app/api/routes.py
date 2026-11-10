"""本地 HTTP 接口。只给本机桌面窗口用。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse, StreamingResponse

from app import __version__
from app.core.editor import EditorError
from app.core.ffmpeg import FFmpegError, ffmpeg_available, resolve_bin
from app.paths import data_dir, exports_dir, video_dir
from app.paths import safe_filename
from app.schemas import CutRequest, JobPublic, SettingPublic, SettingUpdate
from app.services.manager import BusyError, JobNotFound, get_manager

router = APIRouter(prefix="/api")

_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_ALLOWED = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".wmv", ".mpeg", ".mpg", ".ts"}
_MAX_BYTES = 2 * 1024 * 1024 * 1024
_MAX_BATCH = 20


class BatchFileError(BaseModel):
    filename: str
    detail: str


class BatchCreated(BaseModel):
    jobs: list[JobPublic]
    errors: list[BatchFileError]
_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
}


def _check_id(job_id: str) -> str:
    if not _JOB_ID.fullmatch(job_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    return job_id


async def _state(job_id: str):
    try:
        return await get_manager().get(_check_id(job_id))
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc


def _media_type(path: Path) -> str:
    return _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


async def _write_upload(upload: UploadFile, dest: Path) -> None:
    size = 0
    try:
        with dest.open("wb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_BYTES:
                    raise HTTPException(status_code=413, detail="视频超过 2GB 限制")
                handle.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    if size <= 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="视频文件是空的")


def open_in_file_manager(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
        return
    command = ["open", str(path)] if sys.platform == "darwin" else ["xdg-open", str(path)]
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@router.get("/health")
async def health() -> dict:
    payload: dict = {
        "ok": True,
        "version": __version__,
        "data_dir": str(data_dir()),
        "ffmpeg": ffmpeg_available(),
    }
    if payload["ffmpeg"]:
        payload["ffmpeg_path"] = resolve_bin("ffmpeg")
    else:
        payload["ffmpeg_error"] = "未找到 ffmpeg 或 ffprobe"
    return payload


@router.get("/settings", response_model=SettingPublic)
async def read_settings() -> SettingPublic:
    return get_manager().public_settings()


@router.put("/settings", response_model=SettingPublic)
async def write_settings(patch: SettingUpdate) -> SettingPublic:
    return await get_manager().update_settings(patch)


@router.post("/jobs", response_model=JobPublic, response_model_by_alias=True)
async def create_job(
    file: UploadFile = File(...),
    fps: float | None = Form(default=None),
    concurrency: int | None = Form(default=None),
) -> JobPublic:
    return await _accept_upload(file, fps, concurrency)


@router.post("/jobs/batch", response_model=BatchCreated, response_model_by_alias=True)
async def create_jobs(
    files: list[UploadFile] = File(...),
    fps: float | None = Form(default=None),
    concurrency: int | None = Form(default=None),
) -> BatchCreated:
    if not files:
        raise HTTPException(status_code=400, detail="请选择视频")
    if len(files) > _MAX_BATCH:
        raise HTTPException(status_code=400, detail="一次最多上传 20 个视频")
    jobs: list[JobPublic] = []
    errors: list[BatchFileError] = []
    for file in files:
        try:
            jobs.append(await _accept_upload(file, fps, concurrency))
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            errors.append(BatchFileError(filename=file.filename or "", detail=detail))
    if not jobs and errors:
        raise HTTPException(status_code=400, detail=errors[0].detail)
    return BatchCreated(jobs=jobs, errors=errors)


async def _accept_upload(file: UploadFile, fps: float | None, concurrency: int | None) -> JobPublic:
    filename = safe_filename(file.filename or "video.mp4")
    if Path(filename).suffix.lower() not in _ALLOWED:
        raise HTTPException(status_code=400, detail=f"不支持的视频格式：{filename}")
    if fps is not None and not 0.5 <= fps <= 2:
        raise HTTPException(status_code=400, detail="抽帧 FPS 需要在 0.5 到 2 之间")
    if concurrency is not None and not 1 <= concurrency <= 12:
        raise HTTPException(status_code=400, detail="并发数需要在 1 到 12 之间")
    job_id = uuid.uuid4().hex
    directory = video_dir(job_id)
    dest = directory / filename
    directory.mkdir(parents=True, exist_ok=True)
    try:
        await _write_upload(file, dest)
        state = await get_manager().create_job(
            dest,
            filename,
            job_id=job_id,
            fps=fps,
            concurrency=concurrency,
        )
    except BusyError as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HTTPException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(exc)[:300]) from exc
    return state.to_public()


@router.get("/jobs", response_model=list[JobPublic], response_model_by_alias=True)
async def list_jobs() -> list[JobPublic]:
    return await get_manager().list_jobs()


@router.get("/jobs/{job_id}", response_model=JobPublic, response_model_by_alias=True)
async def read_job(job_id: str) -> JobPublic:
    return (await _state(job_id)).to_public()


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    await _state(job_id)

    async def generate():
        async for payload in get_manager().iter_events(job_id):
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/jobs/{job_id}/cancel", response_model=JobPublic, response_model_by_alias=True)
async def cancel_job(job_id: str) -> JobPublic:
    _check_id(job_id)
    try:
        state = await get_manager().cancel(job_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    return state.to_public()


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    _check_id(job_id)
    await get_manager().delete(job_id)
    return {"ok": True}


@router.get("/jobs/{job_id}/media")
async def job_media(job_id: str) -> FileResponse:
    state = await _state(job_id)
    path = Path(state.source_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="视频文件不存在")
    return FileResponse(
        path,
        media_type=_media_type(path),
        filename=state.filename,
        content_disposition_type="inline",
    )


@router.get("/jobs/{job_id}/output")
async def job_output(job_id: str) -> FileResponse:
    state = await _state(job_id)
    if not state.output_path:
        raise HTTPException(status_code=404, detail="还没有剪辑结果")
    path = Path(state.output_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="剪辑文件不存在")
    return FileResponse(path, media_type="video/mp4", filename=path.name, content_disposition_type="inline")


@router.get("/jobs/{job_id}/export")
async def export_job(job_id: str, fmt: str = "json") -> FileResponse:
    if fmt not in {"json", "xlsx"}:
        raise HTTPException(status_code=400, detail="只支持 json 或 xlsx")
    _check_id(job_id)
    try:
        path = await get_manager().export(job_id, fmt)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    media = (
        "application/json"
        if fmt == "json"
        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    return FileResponse(path, media_type=media, filename=path.name)


@router.post("/jobs/{job_id}/cut", response_model=JobPublic, response_model_by_alias=True)
async def cut_job(job_id: str, body: CutRequest) -> JobPublic:
    _check_id(job_id)
    try:
        state = await get_manager().cut(job_id, body.padding_sec)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except (EditorError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FFmpegError as exc:
        raise HTTPException(status_code=500, detail=str(exc)[:300]) from exc
    return state.to_public()


class OpenDirRequest(BaseModel):
    target: str = Field(default="exports", pattern="^(exports|data)$")


@router.post("/system/open-dir")
async def open_dir(body: OpenDirRequest) -> dict:
    path = data_dir() if body.target == "data" else exports_dir()
    try:
        open_in_file_manager(path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"无法打开文件夹：{exc}") from exc
    return {"path": str(path)}
