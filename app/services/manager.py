"""任务状态、历史和识别调度。识别过程中的进度只放在内存里，完成后再写命中。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.core.aggregate import aggregate
from app.core.deepseek import build_recognizer
from app.core.editor import detect_silences, draft_cuts, normalize_cuts, render_clean_video
from app.core.ffmpeg import FFmpegError, extract_strip, probe_video
from app.core.pipeline import JobCancelled, Progress, run_pipeline
from app.core.speech import NoAudio, SpeechProgress, run_speech
from app.core.tencent_asr import MockAsrClient, TencentAsrClient
from app.core.timeutil import utcnow
from app.db import Database
from app.exporting import export_file, write_json, write_xlsx
from app.paths import audio_dir, data_dir, exports_dir, frames_dir, video_dir
from app.schemas import DEFAULT_SETTINGS, CutSpan, DetectionRules, Finding, JobPublic, RawHit, RunConfig, SettingPublic, SettingUpdate

log = logging.getLogger(__name__)

TERMINAL = {"done", "failed", "cancelled"}
_PREVIEWABLE = {".mp4", ".webm", ".m4v"}
_MAX_QUEUE = 20
_strip_locks: dict[str, asyncio.Lock] = {}


class JobNotFound(Exception):
    pass


class BusyError(Exception):
    pass


def mask_key(key: str) -> str:
    if not key:
        return ""
    return f"***{key[-4:]}"


def _preview_hint(path: str) -> str:
    if Path(path).suffix.lower() in _PREVIEWABLE:
        return ""
    return "该封装可能无法在窗口内直接预览，识别结果仍然可用。建议使用 H.264 MP4。"


def _load_edit_plan(raw) -> dict | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("cuts"), list):
        return None
    cuts: list[dict[str, float]] = []
    for item in data["cuts"]:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item["start_sec"])
            end = float(item["end_sec"])
        except (KeyError, TypeError, ValueError):
            continue
        cuts.append({"start_sec": start, "end_sec": end})
    try:
        padding = float(data.get("padding_sec") or 0)
    except (TypeError, ValueError):
        padding = 0.0
    return {"padding_sec": padding, "cuts": cuts, "rendered": bool(data.get("rendered"))}


def _plan_cuts(plan: dict | None) -> list[dict[str, float]]:
    if not plan:
        return []
    return list(plan.get("cuts") or [])


def _plan_rendered(plan: dict | None) -> bool:
    return bool(plan and plan.get("rendered"))


def _plan_payload(padding_sec: float, cuts: list[tuple[float, float]], *, rendered: bool) -> dict:
    return {
        "padding_sec": padding_sec,
        "cuts": [{"start_sec": start, "end_sec": end} for start, end in cuts],
        "rendered": rendered,
    }


@dataclass
class JobState:
    id: str
    filename: str
    source_path: str
    status: str
    stage: str
    message: str
    progress: float
    fps: float
    concurrency: int
    duration_sec: float | None
    frames_estimated: int
    frames_extracted: int
    frames_recognized: int
    failed_frames: int
    hit_count: int
    error: str | None
    speech_stage: str
    speech_message: str
    asr_error: str | None
    options: dict
    output_path: str | None
    created_at: str
    updated_at: str
    finished_at: str | None
    findings: list[Finding] = field(default_factory=list)
    edit_plan: dict | None = None
    visual_findings: list[Finding] = field(default_factory=list)
    speech_hits: list[RawHit] = field(default_factory=list)
    speech_ratio: float = 0.0
    progress_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    listeners: list[asyncio.Queue] = field(default_factory=list)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    holder: dict | None = None
    last_save: float = 0.0

    def touch(self) -> None:
        self.updated_at = utcnow()

    def to_row(self) -> dict:
        return {
            "id": self.id,
            "filename": self.filename,
            "source_path": self.source_path,
            "status": self.status,
            "stage": self.stage,
            "message": self.message,
            "progress": self.progress,
            "fps": self.fps,
            "concurrency": self.concurrency,
            "duration_sec": self.duration_sec,
            "frames_estimated": self.frames_estimated,
            "frames_extracted": self.frames_extracted,
            "frames_recognized": self.frames_recognized,
            "failed_frames": self.failed_frames,
            "hit_count": self.hit_count,
            "error": self.error,
            "speech_stage": self.speech_stage,
            "speech_message": self.speech_message,
            "asr_error": self.asr_error,
            "options_json": json.dumps(self.options, ensure_ascii=False),
            "output_path": self.output_path,
            "edit_plan": json.dumps(self.edit_plan, ensure_ascii=False) if self.edit_plan else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
        }

    def to_public(self, include_findings: bool = True) -> JobPublic:
        output_ready = bool(self.output_path and Path(self.output_path).is_file())
        return JobPublic(
            id=self.id,
            filename=self.filename,
            status=self.status,
            stage=self.stage,
            message=self.message,
            progress=round(self.progress, 1),
            fps=self.fps,
            concurrency=self.concurrency,
            duration_sec=self.duration_sec,
            frames_estimated=self.frames_estimated,
            frames_extracted=self.frames_extracted,
            frames_recognized=self.frames_recognized,
            failed_frames=self.failed_frames,
            hit_count=self.hit_count,
            error=self.error,
            speech_stage=self.speech_stage,
            speech_message=self.speech_message,
            asr_error=self.asr_error,
            mock=bool(self.options.get("mock")),
            output_ready=output_ready,
            preview_hint=_preview_hint(self.source_path),
            cuts=[CutSpan(start_sec=item["start_sec"], end_sec=item["end_sec"]) for item in _plan_cuts(self.edit_plan)],
            edit_rendered=_plan_rendered(self.edit_plan),
            has_edit_plan=self.edit_plan is not None,
            created_at=self.created_at,
            updated_at=self.updated_at,
            finished_at=self.finished_at,
            findings=list(self.findings) if include_findings else [],
        )


def _cell(row, name: str, default):
    if name not in row.keys():
        return default
    return row[name]


def _state_from_row(row, findings: list[Finding]) -> JobState:
    try:
        options = json.loads(row["options_json"] or "{}")
    except json.JSONDecodeError:
        options = {}
    if not isinstance(options, dict):
        options = {}
    return JobState(
        id=row["id"],
        filename=row["filename"],
        source_path=row["source_path"],
        status=row["status"],
        stage=row["stage"],
        message=row["message"] or "",
        progress=row["progress"] or 0,
        fps=row["fps"],
        concurrency=row["concurrency"],
        duration_sec=row["duration_sec"],
        frames_estimated=row["frames_estimated"] or 0,
        frames_extracted=row["frames_extracted"] or 0,
        frames_recognized=row["frames_recognized"] or 0,
        failed_frames=row["failed_frames"] or 0,
        hit_count=row["hit_count"] or 0,
        error=row["error"],
        speech_stage=_cell(row, "speech_stage", ""),
        speech_message=_cell(row, "speech_message", ""),
        asr_error=_cell(row, "asr_error", None),
        options=options,
        output_path=row["output_path"],
        created_at=row["created_at"],
        edit_plan=_load_edit_plan(_cell(row, "edit_plan", None)),
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
        findings=findings,
    )


class JobManager:
    def __init__(self) -> None:
        self.db = Database(data_dir() / "app.db")
        self._settings: dict = dict(DEFAULT_SETTINGS)
        self._jobs: dict[str, JobState] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._gate = asyncio.Lock()
        self._stopping = False

    async def init(self) -> None:
        self.db = Database(data_dir() / "app.db")
        await self.db.connect()
        await self.db.fail_interrupted(utcnow())
        self._settings = await self.db.load_settings()

    async def shutdown(self) -> None:
        self._stopping = True
        for state in self._jobs.values():
            state.cancel_event.set()
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.db.close()

    def public_settings(self) -> SettingPublic:
        key, source = self._effective_key()
        secret_id, secret_key, region, secret_source = self._effective_tencent()
        return SettingPublic(
            has_api_key=bool(key),
            api_key_masked=mask_key(key),
            api_key_source=source,
            base_url=str(self._settings["base_url"]),
            model=str(self._settings["model"]),
            fps=float(self._settings["fps"]),
            concurrency=int(self._settings["concurrency"]),
            max_width=int(self._settings["max_width"]),
            jpeg_q=int(self._settings["jpeg_q"]),
            detail=str(self._settings["detail"]),
            thinking=bool(self._settings["thinking"]),
            timeout_sec=float(self._settings["timeout_sec"]),
            mock_mode=bool(self._settings["mock_mode"]),
            asr_enabled=bool(self._settings.get("asr_enabled", True)),
            has_tencent_secret=bool(secret_id and secret_key),
            tencent_secret_id_masked=mask_key(secret_id),
            tencent_secret_key_masked=mask_key(secret_key),
            tencent_secret_source=secret_source,
            tencent_region=region,
            asr_engine=str(self._settings.get("asr_engine") or "16k_zh"),
            rules=self._rules(),
        )

    async def update_settings(self, patch: SettingUpdate) -> SettingPublic:
        data = dict(self._settings)
        raw = patch.model_dump(exclude_unset=True)
        if raw.get("clear_api_key"):
            data["api_key"] = ""
        if raw.get("api_key"):
            data["api_key"] = raw["api_key"]
        if raw.get("clear_tencent_secret"):
            data["tencent_secret_id"] = ""
            data["tencent_secret_key"] = ""
        if raw.get("tencent_secret_id"):
            data["tencent_secret_id"] = raw["tencent_secret_id"]
        if raw.get("tencent_secret_key"):
            data["tencent_secret_key"] = raw["tencent_secret_key"]
        for name in (
            "base_url",
            "model",
            "fps",
            "concurrency",
            "max_width",
            "jpeg_q",
            "detail",
            "thinking",
            "timeout_sec",
            "mock_mode",
            "asr_enabled",
            "tencent_region",
            "asr_engine",
        ):
            if name in raw and raw[name] is not None:
                data[name] = raw[name]
        if raw.get("rules") is not None:
            data["rules"] = DetectionRules.model_validate(raw["rules"]).model_dump()
        self._settings = data
        await self.db.save_settings(data)
        return self.public_settings()

    def _effective_key(self) -> tuple[str, str]:
        saved = str(self._settings.get("api_key") or "").strip()
        if saved:
            return saved, "saved"
        env_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if env_key:
            return env_key, "env"
        return "", "none"

    def _effective_tencent(self) -> tuple[str, str, str, str]:
        region = str(self._settings.get("tencent_region") or "").strip()
        if not region:
            region = os.environ.get("TENCENTCLOUD_REGION", "").strip() or "ap-shanghai"
        saved_id = str(self._settings.get("tencent_secret_id") or "").strip()
        saved_key = str(self._settings.get("tencent_secret_key") or "").strip()
        if saved_id and saved_key:
            return saved_id, saved_key, region, "saved"
        env_id = os.environ.get("TENCENTCLOUD_SECRET_ID", "").strip()
        env_key = os.environ.get("TENCENTCLOUD_SECRET_KEY", "").strip()
        if env_id and env_key:
            return env_id, env_key, region, "env"
        return "", "", region, "none"

    def _rules(self) -> DetectionRules:
        try:
            return DetectionRules.model_validate(self._settings.get("rules") or {})
        except Exception:
            return DetectionRules()

    def _mock_enabled(self) -> bool:
        return bool(self._settings.get("mock_mode")) or os.environ.get("EVC_MOCK") == "1"

    async def create_job(
        self,
        source: Path,
        filename: str,
        *,
        job_id: str | None = None,
        fps: float | None = None,
        concurrency: int | None = None,
        mock: bool | None = None,
        asr: bool | None = None,
    ) -> JobState:
        if not source.is_file():
            raise FileNotFoundError("视频文件不存在")
        now = utcnow()
        chosen_fps = float(self._settings["fps"] if fps is None else fps)
        chosen_concurrency = int(self._settings["concurrency"] if concurrency is None else concurrency)
        use_mock = self._mock_enabled() if mock is None else mock
        use_asr = bool(self._settings.get("asr_enabled", True)) if asr is None else asr
        options = {
            "fps": chosen_fps,
            "concurrency": chosen_concurrency,
            "max_width": int(self._settings["max_width"]),
            "jpeg_q": int(self._settings["jpeg_q"]),
            "model": str(self._settings["model"]),
            "detail": str(self._settings["detail"]),
            "thinking": bool(self._settings["thinking"]),
            "timeout_sec": float(self._settings["timeout_sec"]),
            "base_url": str(self._settings["base_url"]),
            "mock": use_mock,
            "asr_enabled": use_asr,
            "asr_engine": str(self._settings.get("asr_engine") or "16k_zh"),
            "tencent_region": str(self._settings.get("tencent_region") or "ap-shanghai"),
            "rules": self._rules().model_dump(),
        }
        state = JobState(
            id=job_id or uuid.uuid4().hex,
            filename=filename,
            source_path=str(source),
            status="queued",
            stage="queued",
            message="排队中",
            progress=0,
            fps=chosen_fps,
            concurrency=chosen_concurrency,
            duration_sec=None,
            frames_estimated=0,
            frames_extracted=0,
            frames_recognized=0,
            failed_frames=0,
            hit_count=0,
            error=None,
            speech_stage="",
            speech_message="",
            asr_error=None,
            options=options,
            output_path=None,
            created_at=now,
            updated_at=now,
            finished_at=None,
        )
        async with self._gate:
            pending = sum(1 for job in self._jobs.values() if job.status in {"queued", "running"})
            if pending >= _MAX_QUEUE:
                raise BusyError("排队已满（最多 20 个），请等一部分完成后再上传")
            state.message = "排队中" if pending == 0 else f"排队中，前面还有 {pending} 个"
            self._jobs[state.id] = state
            await self.db.upsert_job(state.to_row())
            inline = os.environ.get("EVC_INLINE_JOBS") == "1"
            if not inline:
                self._kick_locked()
        if inline:
            await self.run(state.id)
        return state

    def _kick_locked(self) -> None:
        if any(job.status == "running" for job in self._jobs.values()):
            return
        if any(not task.done() for task in self._tasks.values()):
            return
        queued = sorted(
            (
                job
                for job in self._jobs.values()
                if job.status == "queued" and not job.cancel_event.is_set()
            ),
            key=lambda job: (job.created_at, job.id),
        )
        if not queued:
            return
        nxt = queued[0]
        self._tasks[nxt.id] = asyncio.create_task(self._run_then_next(nxt.id), name=f"job-{nxt.id}")

    async def _run_then_next(self, job_id: str) -> None:
        try:
            await self.run(job_id)
        finally:
            self._tasks.pop(job_id, None)
            if self._stopping:
                return
            async with self._gate:
                self._kick_locked()

    async def run(self, job_id: str) -> None:
        state = self._jobs[job_id]
        if state.cancel_event.is_set() or state.status in TERMINAL:
            return
        state.status = "running"
        state.stage = "extracting"
        state.message = "准备处理"
        state.touch()
        await self.db.upsert_job(state.to_row())
        self._publish(state)
        holder: dict = {"proc": None, "audio_proc": None, "recognizer": None, "asr": None}
        state.holder = holder
        config = self._config_for(state)
        try:
            recognizer = build_recognizer(config)
            holder["recognizer"] = recognizer

            async def on_progress(progress: Progress) -> None:
                await self._on_visual(state, progress)

            async def on_speech(progress: SpeechProgress) -> None:
                await self._on_speech(state, progress)

            async with recognizer:
                visual_task = asyncio.create_task(
                    run_pipeline(
                        Path(state.source_path),
                        frames_dir(state.id),
                        config,
                        recognizer,
                        state.cancel_event,
                        on_progress,
                        lambda proc: holder.__setitem__("proc", proc),
                    ),
                    name=f"visual-{state.id}",
                )
                speech_task = self._start_speech(state, config, recognizer, holder, on_speech)
                try:
                    findings = await visual_task
                except BaseException:
                    state.cancel_event.set()
                    _kill_processes(holder)
                    if speech_task is not None:
                        speech_task.cancel()
                        await asyncio.gather(speech_task, return_exceptions=True)
                    raise
                speech_hits: list[RawHit] = []
                speech_error = ""
                if speech_task is not None:
                    if not speech_task.done():
                        async with state.progress_lock:
                            if state.status not in TERMINAL:
                                state.stage = "aggregating"
                                state.message = "画面已完成，正在等待口播结果"
                                state.touch()
                        self._publish(state)
                    speech_hits, speech_error = await speech_task
                if state.cancel_event.is_set():
                    raise JobCancelled()
            async with state.progress_lock:
                state.visual_findings = findings
                state.speech_hits = speech_hits
                if speech_error:
                    state.asr_error = speech_error
                    state.speech_stage = "failed"
                    state.speech_message = speech_error
                state.findings = combine_findings(findings, speech_hits)
                state.hit_count = len(state.findings)
                state.status = "done"
                state.stage = "done"
                state.progress = 100
                state.error = None
                extra = f"，{state.failed_frames} 帧识别失败" if state.failed_frames else ""
                if config.asr_enabled:
                    visual_count = sum(1 for item in state.findings if item.source != "口播")
                    oral_count = state.hit_count - visual_count
                    state.message = f"完成，命中 {state.hit_count} 条（画面 {visual_count}，口播 {oral_count}）{extra}"
                else:
                    state.message = f"完成，命中 {state.hit_count} 条{extra}"
                state.touch()
        except JobCancelled:
            async with state.progress_lock:
                state.status = "cancelled"
                state.stage = "cancelled"
                state.message = "已取消"
                state.touch()
        except asyncio.CancelledError:
            async with state.progress_lock:
                state.status = "cancelled"
                state.stage = "cancelled"
                state.message = "已取消"
                state.touch()
            raise
        except Exception as exc:
            log.exception("job %s failed", job_id)
            state.status = "failed"
            state.stage = "failed"
            state.error = str(exc)[:500]
            state.message = state.error or "处理失败"
        finally:
            state.finished_at = state.finished_at or utcnow()
            state.touch()
            state.holder = None
            try:
                await self.db.upsert_job(state.to_row())
                if state.status == "done":
                    await self.db.replace_findings(state.id, state.findings)
            except Exception:
                log.exception("persist job %s failed", job_id)
            self._publish(state)

    def _config_for(self, state: JobState) -> RunConfig:
        key, _source = self._effective_key()
        secret_id, secret_key, region, _secret_source = self._effective_tencent()
        options = state.options
        return RunConfig(
            fps=float(options.get("fps", state.fps)),
            concurrency=int(options.get("concurrency", state.concurrency)),
            max_width=int(options.get("max_width", 1280)),
            jpeg_q=int(options.get("jpeg_q", 5)),
            api_key=key,
            base_url=str(options.get("base_url") or self._settings["base_url"]),
            model=str(options.get("model") or self._settings["model"]),
            detail=str(options.get("detail") or "original"),
            thinking=bool(options.get("thinking", False)),
            timeout_sec=float(options.get("timeout_sec", 60)),
            mock=bool(options.get("mock", False)),
            asr_enabled=bool(options.get("asr_enabled", True)),
            tencent_secret_id=secret_id,
            tencent_secret_key=secret_key,
            tencent_region=str(options.get("tencent_region") or region),
            asr_engine=str(options.get("asr_engine") or "16k_zh"),
            rules=self._rules_from(options),
        )

    def _rules_from(self, options: dict) -> DetectionRules:
        raw = options.get("rules")
        if not raw:
            return self._rules()
        try:
            return DetectionRules.model_validate(raw)
        except Exception:
            return self._rules()

    def _start_speech(self, state: JobState, config: RunConfig, recognizer, holder: dict, on_speech):
        if not config.asr_enabled:
            return None
        if config.mock:
            asr = MockAsrClient()
        elif config.tencent_secret_id and config.tencent_secret_key:
            asr = TencentAsrClient(
                config.tencent_secret_id,
                config.tencent_secret_key,
                config.tencent_region,
                config.asr_engine,
                timeout=max(30.0, config.timeout_sec),
            )
        else:
            message = "未配置腾讯云密钥，已跳过口播检测"

            async def skipped() -> tuple[list[RawHit], str]:
                await on_speech(SpeechProgress(stage="skipped", message=message, hits=[], ratio=1))
                return [], message

            return asyncio.create_task(skipped(), name=f"speech-{state.id}")
        holder["asr"] = asr
        return asyncio.create_task(
            self._speech_job(state, asr, recognizer, holder, on_speech),
            name=f"speech-{state.id}",
        )

    async def _speech_job(self, state, asr, recognizer, holder, on_speech) -> tuple[list[RawHit], str]:
        try:
            async with asr:
                hits = await run_speech(
                    Path(state.source_path),
                    audio_dir(state.id),
                    asr,
                    recognizer,
                    state.cancel_event,
                    on_speech,
                    lambda proc: holder.__setitem__("audio_proc", proc),
                )
            return hits, ""
        except asyncio.CancelledError:
            raise
        except JobCancelled:
            return [], ""
        except NoAudio as exc:
            await on_speech(SpeechProgress(stage="skipped", message=str(exc), hits=[], ratio=1))
            return [], str(exc)
        except Exception as exc:
            log.exception("speech %s failed", state.id)
            message = str(exc)[:500] or "口播检测失败"
            await on_speech(SpeechProgress(stage="failed", message=message, hits=[], ratio=1))
            return [], message

    async def _on_visual(self, state: JobState, progress: Progress) -> None:
        async with state.progress_lock:
            if state.status in TERMINAL:
                return
            state.stage = progress.stage
            state.message = progress.message
            state.duration_sec = progress.duration_sec
            state.frames_estimated = progress.frames_estimated
            state.frames_extracted = progress.frames_extracted
            state.frames_recognized = progress.frames_recognized
            state.failed_frames = progress.failed_frames
            state.visual_findings = progress.findings
            state.findings = combine_findings(progress.findings, state.speech_hits)
            state.hit_count = len(state.findings)
            self._apply_ratio(state)
            state.touch()
            self._publish(state)
            await self._maybe_save(state)

    async def _on_speech(self, state: JobState, progress: SpeechProgress) -> None:
        async with state.progress_lock:
            if state.status in TERMINAL:
                return
            state.speech_stage = progress.stage
            state.speech_message = progress.message
            state.speech_hits = progress.hits
            state.speech_ratio = progress.ratio
            state.findings = combine_findings(state.visual_findings, progress.hits)
            state.hit_count = len(state.findings)
            self._apply_ratio(state)
            state.touch()
            self._publish(state)
            await self._maybe_save(state)

    def _apply_ratio(self, state: JobState) -> None:
        denom = max(state.frames_estimated, 1)
        visual_ratio = state.frames_recognized / denom if state.frames_estimated else 0
        if state.options.get("asr_enabled", True):
            state.progress = min(99.0, (visual_ratio * 0.7 + state.speech_ratio * 0.3) * 100)
        else:
            state.progress = min(99.0, visual_ratio * 100)

    async def _maybe_save(self, state: JobState) -> None:
        now = time.monotonic()
        if now - state.last_save < 0.8:
            return
        state.last_save = now
        try:
            await self.db.upsert_job(state.to_row())
            await self.db.replace_findings(state.id, state.findings)
        except Exception:
            log.exception("progress save failed")

    def _publish(self, state: JobState) -> None:
        for listener in list(state.listeners):
            try:
                listener.put_nowait(1)
            except asyncio.QueueFull:
                pass

    async def get(self, job_id: str) -> JobState:
        cached = self._jobs.get(job_id)
        if cached is not None:
            return cached
        row = await self.db.get_job(job_id)
        if row is None:
            raise JobNotFound(job_id)
        state = _state_from_row(row, await self.db.get_findings(job_id))
        self._jobs[job_id] = state
        return state

    async def list_jobs(self) -> list[JobPublic]:
        rows = await self.db.list_jobs()
        result: list[JobPublic] = []
        for row in rows:
            state = self._jobs.get(row["id"])
            if state is None:
                state = _state_from_row(row, [])
            result.append(state.to_public(include_findings=False))
        return result

    async def iter_events(self, job_id: str):
        state = await self.get(job_id)
        listener: asyncio.Queue = asyncio.Queue(maxsize=1)
        state.listeners.append(listener)
        try:
            yield state.to_public().model_dump(mode="json", by_alias=True)
            while state.status not in TERMINAL:
                await listener.get()
                payload = state.to_public().model_dump(mode="json", by_alias=True)
                yield payload
                if payload["status"] in TERMINAL:
                    break
        finally:
            if listener in state.listeners:
                state.listeners.remove(listener)

    async def cancel(self, job_id: str) -> JobState:
        state = await self.get(job_id)
        if state.status in TERMINAL:
            return state
        state.cancel_event.set()
        if state.status == "queued":
            async with self._gate:
                if state.status == "queued":
                    state.status = "cancelled"
                    state.stage = "cancelled"
                    state.message = "已取消"
                    state.finished_at = state.finished_at or utcnow()
                    state.touch()
                    await self.db.upsert_job(state.to_row())
                    self._publish(state)
                    return state
        holder = state.holder or {}
        _kill_processes(holder)
        recognizer = holder.get("recognizer")
        if recognizer is not None:
            await recognizer.aclose()
        asr = holder.get("asr")
        if asr is not None:
            await asr.aclose()
        return state

    async def delete(self, job_id: str) -> None:
        try:
            state = await self.get(job_id)
        except JobNotFound:
            return
        if state.status not in TERMINAL:
            await self.cancel(job_id)
            task = self._tasks.get(job_id)
            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=8)
                except Exception:
                    task.cancel()
        await self.db.delete_job(job_id)
        self._jobs.pop(job_id, None)
        self._tasks.pop(job_id, None)
        shutil.rmtree(video_dir(job_id), ignore_errors=True)
        shutil.rmtree(frames_dir(job_id), ignore_errors=True)
        shutil.rmtree(audio_dir(job_id), ignore_errors=True)
        _strip_locks.pop(job_id, None)
        for path in exports_dir().glob(f"{job_id}_*"):
            path.unlink(missing_ok=True)

    async def export(self, job_id: str, fmt: str) -> Path:
        state = await self.get(job_id)
        if state.status != "done":
            raise RuntimeError("任务还没完成，不能导出")
        suffix = "json" if fmt == "json" else "xlsx"
        path = export_file(state.id, state.filename, suffix)
        if fmt == "json":
            write_json(path, state.filename, state.duration_sec, state.fps, state.findings)
        else:
            write_xlsx(path, state.filename, state.duration_sec, state.fps, state.findings)
        return path

    async def _editable(self, job_id: str) -> tuple[JobState, Path]:
        state = await self.get(job_id)
        if state.status != "done":
            raise RuntimeError("任务还没完成，不能剪辑")
        source = Path(state.source_path)
        if not source.is_file():
            raise RuntimeError("原视频不存在，无法剪辑")
        return state, source

    async def plan_edit(self, job_id: str, padding_sec: float) -> JobState:
        state, source = await self._editable(job_id)
        if not state.findings:
            raise RuntimeError("没有命中区间，无需剪辑")
        probe = await probe_video(source)
        duration = probe.duration or state.duration_sec or 0
        silences = await detect_silences(source)
        cuts, keeps = draft_cuts(duration, state.findings, padding_sec, silences)
        await self.ensure_strip(job_id, source)
        state.edit_plan = _plan_payload(padding_sec, cuts, rendered=False)
        if keeps:
            state.message = f"已生成剪辑方案，共 {len(cuts)} 段待删除"
        else:
            state.message = "删除段盖住了整段视频。选中一段后，用起点、终点或 0.1 秒按钮把边缘往里收，留下要保留的画面。"
        state.touch()
        await self.db.upsert_job(state.to_row())
        self._publish(state)
        return state

    async def save_cuts(self, job_id: str, spans: list[CutSpan]) -> JobState:
        state, source = await self._editable(job_id)
        duration = state.duration_sec or 0
        if duration <= 0:
            probe = await probe_video(source)
            duration = probe.duration or 0
        padding = 0.2
        if state.edit_plan is not None:
            try:
                padding = float(state.edit_plan.get("padding_sec") or 0)
            except (TypeError, ValueError):
                padding = 0.2
        cuts, _keeps = normalize_cuts(
            duration,
            [(item.start_sec, item.end_sec) for item in spans],
            require_keep=False,
        )
        state.edit_plan = _plan_payload(padding, cuts, rendered=False)
        state.message = f"已保存剪辑方案，共 {len(cuts)} 段待删除"
        state.touch()
        await self.db.upsert_job(state.to_row())
        self._publish(state)
        return state

    async def render_edit(self, job_id: str) -> JobState:
        state, source = await self._editable(job_id)
        plan = state.edit_plan
        if not plan or "cuts" not in plan:
            raise RuntimeError("还没有剪辑方案")
        probe = await probe_video(source)
        duration = probe.duration or state.duration_sec or 0
        cuts, keeps = normalize_cuts(
            duration,
            [(float(item["start_sec"]), float(item["end_sec"])) for item in plan["cuts"]],
        )
        if not cuts:
            raise RuntimeError("没有要删除的区间，无需剪辑")
        output = video_dir(state.id) / "clean.mp4"
        await render_clean_video(source, output, keeps, probe)
        padding = float(plan.get("padding_sec") or 0)
        state.edit_plan = _plan_payload(padding, cuts, rendered=True)
        state.output_path = str(output)
        state.message = f"已生成剪辑视频，保留 {len(keeps)} 段"
        state.touch()
        await self.db.upsert_job(state.to_row())
        self._publish(state)
        return state

    async def cut(self, job_id: str, padding_sec: float) -> JobState:
        await self.plan_edit(job_id, padding_sec)
        return await self.render_edit(job_id)

    async def ensure_strip(self, job_id: str, source: Path) -> None:
        """时间轴缩略图。已经有图就跳过；抽失败时留空，界面退回纯色条。"""
        directory = frames_dir(job_id) / "strip"
        if _strip_ready(directory):
            return
        lock = _strip_locks.setdefault(job_id, asyncio.Lock())
        async with lock:
            if _strip_ready(directory):
                return
            try:
                await extract_strip(source, directory)
            except (FFmpegError, OSError):
                log.warning("timeline strip failed for %s", job_id)

    async def strip_image(self, job_id: str, index: int) -> Path:
        _state, source = await self._editable(job_id)
        if index < 0 or index > 20_000:
            raise FileNotFoundError
        path = frames_dir(job_id) / "strip" / f"strip_{index:04d}.jpg"
        if path.is_file() and path.stat().st_size > 0:
            return path
        await self.ensure_strip(job_id, source)
        if path.is_file() and path.stat().st_size > 0:
            return path
        raise FileNotFoundError


def combine_findings(visual: list[Finding], speech_hits: list[RawHit]) -> list[Finding]:
    merged = list(visual) + aggregate(speech_hits)
    merged.sort(key=lambda item: (item.start_sec, item.source, item.category, item.text))
    return merged


def _strip_ready(directory: Path) -> bool:
    if not directory.is_dir():
        return False
    return any(path.is_file() and path.stat().st_size > 0 for path in directory.glob("strip_*.jpg"))


def _kill_processes(holder: dict) -> None:
    for name in ("proc", "audio_proc"):
        proc = holder.get(name)
        if proc is not None and proc.returncode is None:
            proc.kill()


_manager: JobManager | None = None


def get_manager() -> JobManager:
    global _manager
    if _manager is None:
        _manager = JobManager()
    return _manager


def reset_manager() -> None:
    global _manager
    _manager = None
