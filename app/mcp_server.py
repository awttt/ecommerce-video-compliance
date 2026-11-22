"""本机 MCP。默认不加载；只调用现有的任务接口，不另跑一条流水线。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI

from app import __version__
from app.core.ffmpeg import ffmpeg_available
from app.logging_setup import redact
from app.paths import data_dir
from app.schemas import Finding
from app.services.manager import TERMINAL, AuthRequired, BusyError, JobNotFound, JobState, get_manager

log = logging.getLogger("app.mcp")

_ALLOWED = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".wmv", ".mpeg", ".mpg", ".ts"}
_MAX_BYTES = 2 * 1024 * 1024 * 1024
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_FINDING_LIMIT = 200
_POLL_SEC = 0.7
_ON = {"1", "true", "yes", "on"}
_OFF = {"0", "false", "no", "off"}

_COLUMNS = ("时间点", "类别", "来源", "原文", "位置", "置信度", "判定理由", "备注")


@dataclass
class _Batch:
    id: str
    folder: str
    total: int
    jobs: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    status: str = "running"
    done_count: int = 0
    message: str = "排队中"
    error: str | None = None
    report: dict | None = None
    task: asyncio.Task | None = None


_batches: dict[str, _Batch] = {}


def reset_batches() -> None:
    for batch in _batches.values():
        task = batch.task
        if task is not None and not task.done():
            task.cancel()
    _batches.clear()


def mcp_enabled() -> bool:
    """环境变量优先。没写环境变量时，看数据库里保存的 mcp_enabled。"""
    raw = os.environ.get("EVC_MCP", "").strip().lower()
    if raw in _ON:
        return True
    if raw in _OFF:
        return False
    return _saved_flag()


def _saved_flag() -> bool:
    path = data_dir() / "app.db"
    if not path.is_file():
        return False
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM settings WHERE key='config'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    if not row or not row[0]:
        return False
    try:
        data = json.loads(row[0])
    except json.JSONDecodeError:
        return False
    return bool(data.get("mcp_enabled")) if isinstance(data, dict) else False


def _ok(payload: dict) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def _fail(message: str) -> str:
    return json.dumps({"ok": False, "error": _clean(message)}, ensure_ascii=False)


def _clean(value: object) -> str:
    return redact(str(value or "").strip())[:300]


def _check_id(job_id: str) -> str | None:
    if not _JOB_ID.fullmatch(job_id or ""):
        return "任务不存在"
    return None


def _brief(state: JobState) -> dict:
    return {
        "id": state.id,
        "filename": state.filename,
        "status": state.status,
        "stage": state.stage,
        "message": _clean(state.message),
        "progress": round(state.progress, 1),
        "hit_count": state.hit_count,
        "error": _clean(state.error) if state.error else None,
        "speech_message": _clean(state.speech_message) if state.speech_message else "",
        "asr_error": _clean(state.asr_error) if state.asr_error else None,
        "output_ready": bool(state.output_path and Path(state.output_path).is_file()),
    }


def _finding_row(filename: str, item: Finding) -> dict:
    exported = item.export_dict()
    row = {"视频": filename}
    for name in _COLUMNS:
        row[name] = exported[name]
    row["start_sec"] = item.start_sec
    row["end_sec"] = item.end_sec
    return row


async def health() -> str:
    """查看版本、ffmpeg 和是否已登录。不返回密钥。"""
    manager = get_manager()
    payload = {
        "version": __version__,
        "ffmpeg": ffmpeg_available(),
        "logged_in": manager._relay_logged_in(),
        "mcp": True,
    }
    return _ok(payload)


async def submit_video(path: str, fps: float | None = None, asr: bool | None = None) -> str:
    """提交一个本地视频，立刻返回任务 id。识别需要几分钟，用 get_job 看进度。"""
    try:
        get_manager().ensure_logged_in()
    except AuthRequired as exc:
        return _fail(str(exc))
    try:
        ready, error = _video_file(path)
        if error:
            return _fail(error)
        if fps is not None and not 0.5 <= fps <= 2:
            return _fail("抽帧 FPS 需要在 0.5 到 2 之间")
        state = await get_manager().create_job(ready, ready.name, fps=fps, asr=asr)
    except AuthRequired as exc:
        return _fail(str(exc))
    except BusyError as exc:
        return _fail(str(exc))
    except FileNotFoundError:
        return _fail("视频文件不存在")
    except Exception as exc:
        log.exception("MCP 提交视频失败")
        return _fail(_clean(exc) or "提交失败")
    return _ok({"job_id": state.id, "filename": state.filename, "status": state.status, "message": _clean(state.message)})


async def get_job(job_id: str) -> str:
    """查看一个任务的进度、说明、命中数和错误。"""
    invalid = _check_id(job_id)
    if invalid:
        return _fail(invalid)
    try:
        state = await get_manager().get(job_id)
    except JobNotFound:
        return _fail("任务不存在")
    except Exception as exc:
        log.exception("MCP 查询任务失败")
        return _fail(_clean(exc) or "查询失败")
    return _ok(_brief(state))


async def list_jobs() -> str:
    """列出已有任务的简表，不含命中明细。"""
    try:
        jobs = await get_manager().list_jobs()
    except Exception as exc:
        log.exception("MCP 列出任务失败")
        return _fail(_clean(exc) or "查询失败")
    brief = [
        {
            "id": item.id,
            "filename": item.filename,
            "status": item.status,
            "message": _clean(item.message),
            "progress": item.progress,
            "hit_count": item.hit_count,
            "error": _clean(item.error) if item.error else None,
        }
        for item in jobs
    ]
    return _ok({"jobs": brief})


async def get_findings(job_id: str) -> str:
    """任务完成后返回 8 列命中和起止秒。超过 200 条时改给导出文件路径。"""
    invalid = _check_id(job_id)
    if invalid:
        return _fail(invalid)
    try:
        state = await get_manager().get(job_id)
    except JobNotFound:
        return _fail("任务不存在")
    except Exception as exc:
        log.exception("MCP 读取命中失败")
        return _fail(_clean(exc) or "查询失败")
    if state.status != "done":
        return _fail(state.message or "任务还没完成，不能导出")
    rows = [_finding_row(state.filename, item) for item in state.findings]
    payload: dict = {"job_id": state.id, "filename": state.filename, "findings_total": len(rows)}
    if len(rows) > _FINDING_LIMIT:
        try:
            path = await get_manager().export(state.id, "json")
        except Exception as exc:
            log.exception("MCP 导出命中失败")
            return _fail(_clean(exc) or "导出失败")
        payload["findings_omitted"] = True
        payload["export_path"] = str(path)
        payload["message"] = f"命中 {len(rows)} 条，明细在 {path}"
    else:
        payload["findings"] = rows
    return _ok(payload)


async def cancel_job(job_id: str) -> str:
    """取消一个还在排队或识别中的任务。"""
    invalid = _check_id(job_id)
    if invalid:
        return _fail(invalid)
    try:
        state = await get_manager().cancel(job_id)
    except JobNotFound:
        return _fail("任务不存在")
    except Exception as exc:
        log.exception("MCP 取消任务失败")
        return _fail(_clean(exc) or "取消失败")
    return _ok(_brief(state))


async def export_job(job_id: str, fmt: str = "json") -> str:
    """导出一个已完成的视频。fmt 是 json 或 xlsx，返回本机文件路径。"""
    invalid = _check_id(job_id)
    if invalid:
        return _fail(invalid)
    if fmt not in {"json", "xlsx"}:
        return _fail("只支持 json 或 xlsx")
    try:
        path = await get_manager().export(job_id, fmt)
    except JobNotFound:
        return _fail("任务不存在")
    except RuntimeError as exc:
        return _fail(str(exc))
    except Exception as exc:
        log.exception("MCP 导出失败")
        return _fail(_clean(exc) or "导出失败")
    return _ok({"job_id": job_id, "export_path": str(path), "fmt": fmt})


async def cut_job(job_id: str, padding_sec: float = 0.2) -> str:
    """按画面边距剪掉命中片段，完成后返回成片路径。边距只作用于画面，范围 0 到 2 秒。"""
    invalid = _check_id(job_id)
    if invalid:
        return _fail(invalid)
    try:
        padding = float(padding_sec)
    except (TypeError, ValueError):
        return _fail("边距需要在 0 到 2 秒之间")
    if not 0 <= padding <= 2:
        return _fail("边距需要在 0 到 2 秒之间")
    try:
        state = await get_manager().cut(job_id, padding)
    except JobNotFound:
        return _fail("任务不存在")
    except Exception as exc:
        log.exception("MCP 剪辑失败")
        return _fail(_clean(exc) or "剪辑失败")
    if not state.output_path or not Path(state.output_path).is_file():
        return _fail(state.message or "还没有剪辑结果")
    return _ok({"job_id": state.id, "output_path": state.output_path, "message": _clean(state.message)})


async def scan_folder(
    folder: str,
    fps: float | None = None,
    asr: bool | None = None,
    progress=None,
) -> str:
    """扫描一个文件夹这一层里的视频，排队检测，全部结束后返回批量报告。

    不进入子文件夹。格式不对的文件记在结果里，不丢掉同目录的合法视频。
    排队最多 20 个，多出来的会等空位再入队。报告写到 exports，并在回复里给出摘要。
    若调用中断，用返回的 batch_id 调用 batch_report，不会把这批视频重新入队。
    """
    try:
        return await _scan_folder(folder, fps, asr, progress)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception("MCP 扫描文件夹失败")
        return _fail(_clean(exc) or "扫描失败")


async def batch_report(batch_id: str) -> str:
    """按 batch_id 取同一次扫描的报告。还在跑就返回进度，跑完返回报告。"""
    try:
        batch = _batches.get((batch_id or "").strip())
        if batch is None:
            return _fail("找不到这批任务")
        if batch.status == "running":
            return _ok(
                {
                    "batch_id": batch.id,
                    "folder": batch.folder,
                    "status": "running",
                    "progress": batch.message,
                    "done": batch.done_count,
                    "total": batch.total,
                }
            )
        if batch.report is not None:
            return _ok(batch.report)
        return _fail(batch.error or "这批任务没有完成")
    except Exception as exc:
        log.exception("MCP 读取批量报告失败")
        return _fail(_clean(exc) or "查询失败")


def _video_file(path: str) -> tuple[Path | None, str | None]:
    try:
        ready = Path(path).expanduser().resolve()
    except OSError:
        return None, "视频文件不存在"
    if not ready.is_file():
        return None, "视频文件不存在"
    if ready.suffix.lower() not in _ALLOWED:
        return None, f"不支持的视频格式：{ready.name}"
    try:
        size = ready.stat().st_size
    except OSError:
        return None, "视频文件不存在"
    if size <= 0:
        return None, "视频文件是空的"
    if size > _MAX_BYTES:
        return None, "视频超过 2GB 限制"
    return ready, None


def _collect(folder: Path) -> tuple[list[Path], list[dict]]:
    videos: list[Path] = []
    skipped: list[dict] = []
    for entry in sorted(folder.iterdir(), key=lambda item: item.name):
        if entry.name.startswith("."):
            continue
        if not entry.is_file():
            continue
        try:
            resolved = entry.resolve()
        except OSError:
            skipped.append({"filename": entry.name, "detail": "无法读取这个文件"})
            continue
        if resolved.parent != folder:
            skipped.append({"filename": entry.name, "detail": "不使用文件夹以外的链接"})
            continue
        if resolved.suffix.lower() not in _ALLOWED:
            skipped.append({"filename": entry.name, "detail": f"不支持的视频格式：{entry.name}"})
            continue
        videos.append(resolved)
    return videos, skipped


async def _scan_folder(folder: str, fps: float | None, asr: bool | None, progress) -> str:
    if fps is not None and not 0.5 <= fps <= 2:
        return _fail("抽帧 FPS 需要在 0.5 到 2 之间")
    try:
        root = Path(folder).expanduser().resolve()
    except OSError:
        return _fail("找不到这个文件夹")
    if not root.is_dir():
        return _fail("找不到这个文件夹")
    try:
        get_manager().ensure_logged_in()
    except AuthRequired as exc:
        return _fail(str(exc))
    videos, skipped = _collect(root)
    if not videos:
        return _fail("这个文件夹里没有可检测的视频")
    batch = _Batch(id=uuid.uuid4().hex, folder=str(root), total=len(videos), skipped=skipped)
    _batches[batch.id] = batch
    batch.task = asyncio.create_task(_drive(batch, videos, fps, asr, progress), name=f"mcp-batch-{batch.id}")
    try:
        await asyncio.shield(batch.task)
    except asyncio.CancelledError:
        raise
    if batch.report is not None:
        return _ok(batch.report)
    return _fail(batch.error or "这批任务没有完成")


async def _drive(batch: _Batch, videos: list[Path], fps: float | None, asr: bool | None, progress) -> None:
    pending = list(videos)
    stop_early: str | None = None
    try:
        while pending:
            path = pending[0]
            try:
                state = await get_manager().create_job(path, path.name, fps=fps, asr=asr)
            except BusyError:
                await _publish(batch, progress)
                await asyncio.sleep(_POLL_SEC)
                continue
            except AuthRequired as exc:
                stop_early = str(exc)
                break
            except Exception as exc:
                log.exception("MCP 无法提交 %s", path.name)
                batch.jobs.append({"filename": path.name, "job_id": None, "error": _clean(exc) or "提交失败"})
                pending.pop(0)
                await _publish(batch, progress)
                continue
            batch.jobs.append({"filename": state.filename, "job_id": state.id, "error": None})
            pending.pop(0)
            await _publish(batch, progress)
        if stop_early:
            for path in pending:
                batch.jobs.append({"filename": path.name, "job_id": None, "error": stop_early})
            if not any(item.get("job_id") for item in batch.jobs):
                batch.status = "failed"
                batch.error = stop_early
                batch.message = stop_early
                return
        while True:
            finished = await _finished(batch)
            batch.done_count = finished
            await _publish(batch, progress)
            if batch.jobs and finished >= len(batch.jobs):
                break
            await asyncio.sleep(_POLL_SEC)
        if stop_early:
            batch.error = stop_early
        batch.report = await _build_report(batch)
        batch.status = "done"
        batch.message = str(batch.report.get("message") or "已完成")
    except asyncio.CancelledError:
        batch.status = "cancelled"
        batch.error = "已取消"
        batch.message = "已取消"
        raise
    except Exception as exc:
        log.exception("MCP 批量任务失败")
        batch.status = "failed"
        batch.error = _clean(exc) or "批量检测失败"
        batch.message = batch.error


async def _finished(batch: _Batch) -> int:
    count = 0
    manager = get_manager()
    for item in batch.jobs:
        job_id = item.get("job_id")
        if not job_id:
            count += 1
            continue
        try:
            state = await manager.get(job_id)
        except JobNotFound:
            count += 1
            continue
        if state.status in TERMINAL:
            count += 1
    return count


async def _publish(batch: _Batch, progress) -> None:
    finished = await _finished(batch)
    batch.done_count = finished
    batch.message = f"已完成 {finished}/{batch.total}"
    if progress is None:
        return
    try:
        await progress(finished, batch.total, batch.message)
    except Exception:
        log.debug("MCP 进度通知没有发出", exc_info=True)


async def _build_report(batch: _Batch) -> dict:
    manager = get_manager()
    summary: list[dict] = []
    rows: list[dict] = []
    ids: list[str] = []
    for item in batch.jobs:
        job_id = item.get("job_id")
        if not job_id:
            summary.append(
                {
                    "filename": item["filename"],
                    "job_id": None,
                    "status": "failed",
                    "hit_count": 0,
                    "error": item.get("error"),
                    "message": item.get("error") or "提交失败",
                    "asr_error": None,
                }
            )
            continue
        ids.append(job_id)
        try:
            state = await manager.get(job_id)
        except JobNotFound:
            summary.append(
                {
                    "filename": item["filename"],
                    "job_id": job_id,
                    "status": "failed",
                    "hit_count": 0,
                    "error": "任务不存在",
                    "message": "任务不存在",
                    "asr_error": None,
                }
            )
            continue
        summary.append(
            {
                "filename": state.filename,
                "job_id": state.id,
                "status": state.status,
                "hit_count": state.hit_count if state.status == "done" else 0,
                "error": _clean(state.error) if state.error else None,
                "message": _clean(state.message),
                "asr_error": _clean(state.asr_error) if state.asr_error else None,
            }
        )
        if state.status == "done":
            rows.extend(_finding_row(state.filename, finding) for finding in state.findings)
    export_path: str | None = None
    export_error: str | None = None
    if ids:
        try:
            export_path = str(await manager.export_batch(ids))
        except RuntimeError as exc:
            export_error = str(exc)
        except Exception as exc:
            log.exception("MCP 批量导出失败")
            export_error = _clean(exc) or "导出失败"
    done_videos = sum(1 for item in summary if item["status"] == "done")
    if export_path and len(rows) <= _FINDING_LIMIT:
        message = f"已完成 {done_videos} 个视频，报告在 {export_path}"
    elif export_path:
        message = f"命中 {len(rows)} 条，明细在 {export_path}"
    elif batch.error:
        message = batch.error
    elif export_error:
        message = export_error
    elif done_videos == 0:
        message = next((item["error"] for item in summary if item.get("error")), "这批视频都没有检测完成")
    else:
        message = "这批视频还没有检测完成"
    payload: dict = {
        "batch_id": batch.id,
        "folder": batch.folder,
        "status": "done",
        "summary": summary,
        "skipped": list(batch.skipped),
        "hit_count": len(rows),
        "findings_total": len(rows),
        "export_path": export_path,
        "message": message,
    }
    if len(rows) > _FINDING_LIMIT:
        payload["findings_omitted"] = True
    else:
        payload["findings"] = rows
    return payload


def attach_mcp(app: FastAPI) -> None:
    """把 Streamable HTTP 挂到 /mcp。调用方要在应用生命周期里启动会话。"""
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.context import Context

    globals()["Context"] = Context
    server = MCPServer(
        name="御镜视频审核系统",
        instructions=(
            "本机视频合规检测。桌面程序需要已经登录。"
            "提交一个视频用 submit_video，再用 get_job 看进度，完成后用 get_findings。"
            "扫描一个文件夹用 scan_folder，它会等这批全部结束再返回报告；中断后用 batch_report 取同一份，不要重新扫描。"
        ),
    )
    for fn in (health, submit_video, get_job, list_jobs, get_findings, cancel_job, export_job, cut_job, batch_report):
        server.tool()(fn)

    async def scan_folder_tool(
        folder: str,
        fps: float | None = None,
        asr: bool | None = None,
        ctx: Context | None = None,
    ) -> str:
        async def progress(done: int, total: int, message: str) -> None:
            if ctx is None:
                return
            await ctx.report_progress(done, total, message)

        return await scan_folder(folder, fps=fps, asr=asr, progress=progress)

    scan_folder_tool.__name__ = "scan_folder"
    scan_folder_tool.__doc__ = scan_folder.__doc__
    server.tool()(scan_folder_tool)

    from mcp.server.transport_security import TransportSecuritySettings

    # 挂成主应用上的 /mcp，避免再套一层前缀变成 /mcp/mcp 或 /mcp/。
    sub = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        host="127.0.0.1",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "127.0.0.1", "localhost", "[::1]"],
            allowed_origins=[
                "http://127.0.0.1:*",
                "http://localhost:*",
                "http://[::1]:*",
                "http://127.0.0.1",
                "http://localhost",
                "http://[::1]",
            ],
        ),
    )
    app.router.routes.extend(sub.routes)
    app.state.mcp_session = server.session_manager.run()
    log.info("MCP 已挂到 /mcp")
