"""任务状态、历史和识别调度。识别过程中的进度只放在内存里，完成后再写命中。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.core.aggregate import aggregate, refold_findings
from app.core.deepseek import build_recognizer
from app.core.model_route import ModelRoute, initial_route_fields, public_stats
from app.core.image_contract import IMAGE_KEY_NAME, apply_preview_login, clear_image_session
from app.core.relay import (
    CHAT_BASE,
    KEY_NAME,
    KEY_NOT_READY,
    RelayClient,
    RelayError,
    configured_model,
    groups_lookup_failed_message,
    relay_group_id_fallback,
    relay_group_name,
    resolve_relay_group_id,
    usable_api_key,
)
from app.logging_setup import redact
from app.core.editor import cuttable_findings, detect_silences, draft_cuts, normalize_cuts, render_clean_video
from app.core.ffmpeg import FFmpegError, extract_strip, probe_video, transcode_preview, window_can_play
from app.core.pipeline import JobCancelled, Progress, drop_retried_reviews, run_pipeline, run_selected_frames
from app.core.speech import NoAudio, SpeechProgress, judge_transcript, run_speech
from app.core.transcript import dump_sentences, load_sentences
from app.core.asr_engine import configured_engine
from app.core.tencent_asr import (
    ASR_MAX_RETRIES,
    AsrCredentials,
    FaultAsrClient,
    MockAsrClient,
    TencentAsrClient,
    parse_relay_credentials,
    retry_backoff_seconds,
    wait_backoff,
)
from app.core.timeutil import utcnow
from app.db import Database
from app.exporting import BatchExportJob, batch_export_file, export_file, write_batch_xlsx, write_json, write_xlsx
from app.paths import audio_dir, data_dir, exports_dir, frames_dir, video_dir
from app.schemas import (
    FAULT_FIELDS,
    TEST_ACCOUNT_EMAIL,
    DEFAULT_SETTINGS,
    FIXED_TENCENT_REGION,
    AccountAction,
    AccountOptions,
    CutSpan,
    RechargeInfo,
    RechargeMethod,
    RechargeOrder,
    DetectionRules,
    Finding,
    JobPublic,
    RawHit,
    RunConfig,
    SettingPublic,
    SettingUpdate,
    clamp_concurrency,
)

log = logging.getLogger(__name__)

TERMINAL = {"done", "failed", "cancelled"}
_MAX_QUEUE = 20
_RELAY_RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
_strip_locks: dict[str, asyncio.Lock] = {}
_preview_locks: dict[str, asyncio.Lock] = {}


def _relay_credential_retryable(exc: RelayError) -> bool:
    """中转站临时故障可以重试。401 和 403（没有口播分组）不行。"""
    status = int(exc.status)
    if status in {401, 403}:
        return False
    return status in _RELAY_RETRY_STATUSES or status >= 500


class JobNotFound(Exception):
    pass


class AuthRequired(Exception):
    pass


class BusyError(Exception):
    pass


def mask_key(key: str) -> str:
    if not key:
        return ""
    return f"***{key[-4:]}"


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number:
            return None
        return number
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


_RELAY_FIELDS = (
    "relay_email",
    "relay_access_token",
    "relay_refresh_token",
    "relay_expires_at",
    "relay_api_key",
    "relay_api_key_id",
    "relay_balance",
    "relay_frozen_balance",
    "relay_today_cost",
    "relay_total_cost",
    "relay_login_at",
)
# 登录态只保留 7 天。上次邮箱单独记下，过期或退出后表单还用它。
SESSION_KEEP_SECONDS = 7 * 24 * 60 * 60


def _email_ok(value: str) -> str:
    cleaned = value.strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", cleaned):
        raise RelayError("邮箱格式不正确", 400)
    return cleaned


def preview_file(job_id: str) -> Path:
    return video_dir(job_id) / "preview.mp4"


def _preview_hint(status: str) -> str:
    if status == "pending":
        return "原片窗口播不了，正在生成预览。识别仍用原片。"
    if status == "ready":
        return "窗口播放的是预览副本，识别仍用原片。"
    if status == "failed":
        return "原片窗口播不了，预览没有生成。识别仍用原片。"
    return ""


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
    started_at: str | None = None
    paused_total_sec: float = 0
    hold_sec: float = 0
    paused_at: str | None = None
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
    failed_indexes: list[int] = field(default_factory=list)
    last_frame_error: str = ""
    retry_failed: bool = False
    retry_part: str = ""
    speech_transcript: list[dict] = field(default_factory=list)
    speech_judge: str = ""
    hold_failed_indexes: bool = False
    queue_at: str = ""
    preview_status: str = ""
    preview_cancel: asyncio.Event = field(default_factory=asyncio.Event)
    preview_task: asyncio.Task | None = None
    preview_proc: asyncio.subprocess.Process | None = None

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
            "failed_frames_json": json.dumps(self.failed_indexes, ensure_ascii=False),
            "hit_count": self.hit_count,
            "error": self.error,
            "speech_stage": self.speech_stage,
            "speech_message": self.speech_message,
            "asr_error": self.asr_error,
            "speech_transcript_json": json.dumps(self.speech_transcript, ensure_ascii=False),
            "speech_judge": self.speech_judge,
            "retry_part": self.retry_part,
            "options_json": json.dumps(self.options, ensure_ascii=False),
            "output_path": self.output_path,
            "edit_plan": json.dumps(self.edit_plan, ensure_ascii=False) if self.edit_plan else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "started_at": self.started_at,
            "paused_total_sec": self.paused_total_sec,
            "hold_sec": self.hold_sec,
            "paused_at": self.paused_at,
        }

    def to_public(self, include_findings: bool = True) -> JobPublic:
        output_ready = bool(self.output_path and Path(self.output_path).is_file())
        preview_status = self.preview_status
        preview_path = preview_file(self.id)
        if preview_status != "direct" and preview_path.is_file() and preview_path.stat().st_size > 0:
            preview_status = "ready"
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
            speech_retry=speech_retry_kind(self),
            mock=bool(self.options.get("mock")),
            output_ready=output_ready,
            preview_status=preview_status,
            preview_hint=_preview_hint(preview_status),
            cuts=[CutSpan(start_sec=item["start_sec"], end_sec=item["end_sec"]) for item in _plan_cuts(self.edit_plan)],
            edit_rendered=_plan_rendered(self.edit_plan),
            has_edit_plan=self.edit_plan is not None,
            created_at=self.created_at,
            updated_at=self.updated_at,
            finished_at=self.finished_at,
            started_at=self.started_at,
            paused_total_sec=self.paused_total_sec,
            hold_sec=self.hold_sec,
            paused_at=self.paused_at,
            findings=list(self.findings) if include_findings else [],
            model_stats=public_stats(self.options.get("model_stats")),
        )


def _cell(row, name: str, default):
    if name not in row.keys():
        return default
    return row[name]


def _failed_indexes(raw) -> list[int]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    indexes: list[int] = []
    for item in data:
        if isinstance(item, bool):
            continue
        if isinstance(item, int) and item > 0:
            indexes.append(item)
        elif isinstance(item, float) and item.is_integer() and item > 0:
            indexes.append(int(item))
    return indexes


def _retry_part(raw) -> str:
    text = str(raw or "")
    if text in {"frames", "transcribe", "judge"}:
        return text
    return ""


def _load_transcript(raw) -> list[dict]:
    return dump_sentences(load_sentences(raw))


def _clear_speech_assignments(options: dict) -> None:
    """重新转写后的口播批次是新内容。清掉旧序号，避免沿用上一份转写的模型。"""
    assignments = options.get("model_assignments")
    if isinstance(assignments, dict) and isinstance(assignments.get("speech"), dict):
        assignments["speech"] = {}


def speech_retry_kind(state: JobState) -> str:
    """结束后还能单独补的那一档。转写文本不发给前端。"""
    if state.status not in {"done", "failed"}:
        return ""
    if not bool(state.options.get("asr_enabled", True)):
        return ""
    if _speech_unretryable(state):
        return ""
    if state.speech_transcript:
        if state.speech_judge == "ok":
            return ""
        if state.speech_judge in {"partial", "failed"} or "已按词表保留" in (state.speech_message or ""):
            return "judge"
        if state.speech_stage in {"judging", "failed"} or state.status == "failed":
            return "judge"
        return ""
    if "已按词表保留" in (state.speech_message or ""):
        return "transcribe"
    if state.speech_stage == "failed":
        return "transcribe"
    if state.status == "failed" and state.speech_stage in {"audio", "submitting", "transcribing"}:
        return "transcribe"
    return ""


def _speech_unretryable(state: JobState) -> bool:
    if state.speech_stage == "skipped":
        return True
    text = f"{state.speech_message or ''}\n{state.asr_error or ''}"
    return "没有音轨" in text or "未登录" in text


def _partial_frame_note(count: int, detail: str) -> str:
    if count <= 0:
        return ""
    note = f"，部分帧识别失败（{count} 帧）"
    cleaned = " ".join(str(detail or "").split())
    if cleaned:
        note = f"{note}。{cleaned[:180]}"
    return note


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
        speech_transcript=_load_transcript(_cell(row, "speech_transcript_json", "[]")),
        speech_judge=str(_cell(row, "speech_judge", "") or ""),
        retry_part=_retry_part(_cell(row, "retry_part", "")),
        options=options,
        output_path=row["output_path"],
        created_at=row["created_at"],
        edit_plan=_load_edit_plan(_cell(row, "edit_plan", None)),
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
        started_at=_cell(row, "started_at", None),
        paused_total_sec=float(_cell(row, "paused_total_sec", 0) or 0),
        hold_sec=float(_cell(row, "hold_sec", 0) or 0),
        paused_at=_cell(row, "paused_at", None) or None,
        findings=findings,
        failed_indexes=_failed_indexes(_cell(row, "failed_frames_json", "[]")),
    )


def _precise_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _parse_stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _seconds_between(start: str, end: str) -> float:
    opened = _parse_stamp(start)
    closed = _parse_stamp(end)
    if opened is None or closed is None:
        return 0
    return max(0.0, (closed - opened).total_seconds())


def _fold_pause(state: JobState, at: str, *, terminal: bool = False) -> None:
    """把一段暂停折进耗时。已经开始的任务从自己的计时里扣掉；还没开始的只记在整批墙上。"""
    if not state.paused_at:
        return
    gap = _seconds_between(state.paused_at, at)
    state.hold_sec = round(state.hold_sec + gap, 3)
    if state.started_at:
        started = _parse_stamp(state.started_at)
        opened = _parse_stamp(state.paused_at)
        closed = _parse_stamp(at)
        if started is not None and opened is not None and closed is not None:
            state.paused_total_sec = round(
                state.paused_total_sec + max(0.0, (closed - max(started, opened)).total_seconds()),
                3,
            )
    elif terminal:
        state.paused_total_sec = round(state.paused_total_sec + gap, 3)
    state.paused_at = None


def _apply_refold(state: JobState) -> None:
    folded = refold_findings(state.findings, state.fps)
    state.findings = folded
    if folded:
        state.hit_count = len(folded)


class JobManager:
    def __init__(self) -> None:
        self.db = Database(data_dir() / "app.db")
        self._settings: dict = dict(DEFAULT_SETTINGS)
        self._jobs: dict[str, JobState] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._gate = asyncio.Lock()
        self._queue_paused = False
        self._pause_mark: str | None = None
        self._resume = asyncio.Event()
        self._resume.set()
        self._account_lock = asyncio.Lock()
        self.relay = RelayClient()
        self._pending_2fa: dict[str, str] | None = None
        self._stopping = False

    async def init(self) -> None:
        self.db = Database(data_dir() / "app.db")
        await self.db.connect()
        was_paused = await self.db.has_status("paused")
        await self.db.fail_interrupted(utcnow())
        self._settings = await self.db.load_settings()
        if apply_preview_login(self._settings):
            await self.db.save_settings(self._settings)
        await self._expire_login_if_needed()
        from app.services.image_jobs import sweep_interrupted

        await sweep_interrupted()
        if was_paused or bool(self._settings.get("queue_paused")):
            self._queue_paused = True
            self._resume.clear()
            self._pause_mark = _precise_now()
        await self._restore_queued()
        if not self._queue_paused:
            async with self._gate:
                self._kick_locked()

    async def _restore_queued(self) -> None:
        rows = await self.db.list_jobs()
        for row in rows:
            if row["status"] != "queued" or row["id"] in self._jobs:
                continue
            findings = await self.db.get_findings(row["id"])
            state = _state_from_row(row, findings)
            _apply_refold(state)
            self._jobs[state.id] = state

    async def shutdown(self) -> None:
        self._stopping = True
        previews: list[asyncio.Task] = []
        for state in self._jobs.values():
            state.cancel_event.set()
            state.preview_cancel.set()
            proc = state.preview_proc
            if proc is not None and proc.returncode is None:
                proc.kill()
            if state.preview_task is not None and not state.preview_task.done():
                state.preview_task.cancel()
                previews.append(state.preview_task)
        if previews:
            await asyncio.gather(*previews, return_exceptions=True)
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        from app.services.image_jobs import shutdown_image_jobs

        await shutdown_image_jobs()
        await self.db.close()

    def relay_credentials(self) -> tuple[str, str]:
        """工具端登录后的中转站令牌。只给同进程的控制台做单点登录，不经过 HTTP。"""
        if not self._relay_logged_in():
            return "", ""
        return (
            str(self._settings.get("relay_access_token") or ""),
            str(self._settings.get("relay_refresh_token") or ""),
        )

    async def prepare_settings(self) -> SettingPublic:
        """满 7 天的登录态在读设置时清掉，邮箱仍留给登录表单。"""
        if self._login_expired() and str(self._settings.get("relay_access_token") or "").strip():
            async with self._account_lock:
                if self._login_expired() and str(self._settings.get("relay_access_token") or "").strip():
                    await self._drop_session()
        return self.public_settings()

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
            concurrency=clamp_concurrency(self._settings.get("concurrency")),
            max_width=int(self._settings["max_width"]),
            jpeg_q=int(self._settings["jpeg_q"]),
            detail=str(self._settings["detail"]),
            thinking=bool(self._settings["thinking"]),
            motion_enabled=bool(self._settings.get("motion_enabled", False)),
            timeout_sec=float(self._settings["timeout_sec"]),
            mock_mode=self._visible_mock(),
            fault_bad_json=self._visible_fault("fault_bad_json"),
            fault_tencent=self._visible_fault("fault_tencent"),
            fault_partial_frames=self._visible_fault("fault_partial_frames"),
            mcp_enabled=bool(self._settings.get("mcp_enabled", False)),
            asr_enabled=bool(self._settings.get("asr_enabled", True)),
            has_tencent_secret=bool(secret_id and secret_key),
            tencent_secret_id_masked=mask_key(secret_id),
            tencent_secret_key_masked=mask_key(secret_key),
            tencent_secret_source=secret_source,
            tencent_region=region,
            asr_engine=configured_engine(self._settings.get("asr_engine")),
            rules=self._rules(),
            account_logged_in=self._relay_logged_in(),
            account_email=self._account_email(),
            remembered_email=self._remembered_email(),
            account_balance=self._account_number("relay_balance"),
            account_frozen_balance=self._account_number("relay_frozen_balance"),
            account_today_cost=self._account_number("relay_today_cost"),
            account_total_cost=self._account_number("relay_total_cost"),
        )

    async def update_settings(self, patch: SettingUpdate) -> SettingPublic:
        async with self._account_lock:
            return await self._update_settings(patch)

    async def _update_settings(self, patch: SettingUpdate) -> SettingPublic:
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
            "fps",
            "concurrency",
            "max_width",
            "jpeg_q",
            "detail",
            "thinking",
            "motion_enabled",
            "timeout_sec",
            "mcp_enabled",
            "model",
            "asr_enabled",
            "asr_engine",
        ):
            if name in raw and raw[name] is not None:
                data[name] = raw[name]
        for name in ("mock_mode", *FAULT_FIELDS):
            if name not in raw or raw[name] is None:
                continue
            if raw[name] and not self._is_test_account():
                continue
            data[name] = bool(raw[name])
        data["tencent_region"] = FIXED_TENCENT_REGION
        if raw.get("rules") is not None:
            data["rules"] = DetectionRules.model_validate(raw["rules"]).model_dump()
        self._settings = data
        await self.db.save_settings(data)
        return self.public_settings()

    def _login_expired(self) -> bool:
        started = _number(self._settings.get("relay_login_at"))
        if started is None:
            return False
        return time.time() >= started + SESSION_KEEP_SECONDS

    def _relay_logged_in(self) -> bool:
        token = str(self._settings.get("relay_access_token") or "").strip()
        email = str(self._settings.get("relay_email") or "").strip()
        if not token or not email or self._login_expired():
            return False
        return True

    def _remembered_email(self) -> str:
        saved = str(self._settings.get("last_login_email") or "").strip()
        if saved:
            return saved
        return str(self._settings.get("relay_email") or "").strip()

    async def _expire_login_if_needed(self) -> None:
        """已有登录但没有记下时间的，从这次启动开始算 7 天。超过 7 天就清登录态。"""
        token = str(self._settings.get("relay_access_token") or "").strip()
        if not token:
            return
        if _number(self._settings.get("relay_login_at")) is None:
            self._settings["relay_login_at"] = time.time()
            email = str(self._settings.get("relay_email") or "").strip()
            if email and not str(self._settings.get("last_login_email") or "").strip():
                self._settings["last_login_email"] = email
            await self.db.save_settings(self._settings)
            return
        if self._login_expired():
            await self._drop_session()

    def ensure_logged_in(self) -> None:
        if not self._relay_logged_in():
            raise AuthRequired("请先登录")

    def _account_email(self) -> str:
        if not self._relay_logged_in():
            return ""
        return str(self._settings.get("relay_email") or "")

    def _account_number(self, name: str) -> float | None:
        if not self._relay_logged_in():
            return None
        return _number(self._settings.get(name))

    def _effective_key(self) -> tuple[str, str]:
        if self._relay_logged_in():
            relay = str(self._settings.get("relay_api_key") or "").strip()
            if relay:
                return relay, "account"
        saved = str(self._settings.get("api_key") or "").strip()
        if saved:
            return saved, "saved"
        env_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if env_key:
            return env_key, "env"
        return "", "none"

    def _effective_tencent(self) -> tuple[str, str, str, str]:
        region = FIXED_TENCENT_REGION
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

    def _is_test_account(self) -> bool:
        return self._account_email().strip().lower() == TEST_ACCOUNT_EMAIL

    def _visible_mock(self) -> bool:
        return bool(self._settings.get("mock_mode")) and self._is_test_account()

    def _visible_fault(self, name: str) -> bool:
        return bool(self._settings.get(name)) and self._is_test_account()

    def _mock_enabled(self) -> bool:
        if os.environ.get("EVC_MOCK") == "1":
            return True
        return self._visible_mock()

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
        self.ensure_logged_in()
        if not source.is_file():
            raise FileNotFoundError("视频文件不存在")
        now = utcnow()
        chosen_fps = float(self._settings["fps"] if fps is None else fps)
        chosen_concurrency = clamp_concurrency(
            self._settings["concurrency"] if concurrency is None else concurrency
        )
        use_mock = self._mock_enabled() if mock is None else mock
        use_asr = bool(self._settings.get("asr_enabled", True)) if asr is None else asr
        options = {
            "fps": chosen_fps,
            "concurrency": chosen_concurrency,
            "max_width": int(self._settings["max_width"]),
            "jpeg_q": int(self._settings["jpeg_q"]),
            "model": str(self._settings["model"]),
            **initial_route_fields(str(self._settings["model"])),
            "detail": str(self._settings["detail"]),
            "thinking": bool(self._settings["thinking"]),
            "motion_enabled": bool(self._settings.get("motion_enabled", False)),
            "timeout_sec": float(self._settings["timeout_sec"]),
            "base_url": str(self._settings["base_url"]),
            "mock": use_mock,
            "asr_enabled": use_asr,
            "asr_engine": configured_engine(self._settings.get("asr_engine")),
            "tencent_region": FIXED_TENCENT_REGION,
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
            pending = sum(1 for job in self._jobs.values() if job.status in {"queued", "running", "paused"})
            if pending >= _MAX_QUEUE:
                raise BusyError("排队已满（最多 20 个），请等一部分完成后再上传")
            state.message = "排队中" if pending == 0 else f"排队中，前面还有 {pending} 个"
            if self._queue_paused:
                state.paused_at = self._pause_mark or _precise_now()
            self._jobs[state.id] = state
            await self.db.upsert_job(state.to_row())
            inline = os.environ.get("EVC_INLINE_JOBS") == "1"
            if not inline:
                self._kick_locked()
        await self.ensure_preview(state)
        if inline:
            await self.run(state.id)
        return state

    async def ensure_preview(self, state: JobState) -> None:
        """窗口播不了的原片转一份 H.264 预览。已经知道结果就直接返回。"""
        if self._stopping or state.preview_status in {"direct", "ready", "pending", "failed"}:
            return
        preview = preview_file(state.id)
        if preview.is_file() and preview.stat().st_size > 0:
            state.preview_status = "ready"
            return
        lock = _preview_locks.setdefault(state.id, asyncio.Lock())
        async with lock:
            if state.preview_status in {"direct", "ready", "pending", "failed"}:
                return
            if preview.is_file() and preview.stat().st_size > 0:
                state.preview_status = "ready"
                return
            source = Path(state.source_path)
            if not source.is_file():
                state.preview_status = "failed"
                return
            try:
                probe = await probe_video(source)
            except (FFmpegError, OSError):
                log.warning("preview probe failed for %s", state.id)
                state.preview_status = "failed"
                return
            if window_can_play(source, probe):
                state.preview_status = "direct"
                return
            if state.preview_cancel.is_set():
                state.preview_status = "failed"
                return
            state.preview_status = "pending"
            state.preview_task = asyncio.create_task(
                self._transcode_preview(state, probe.has_audio),
                name=f"preview-{state.id}",
            )
            self._publish(state)

    async def _transcode_preview(self, state: JobState, has_audio: bool) -> None:
        dest = preview_file(state.id)
        partial = dest.with_name(dest.stem + ".part.mp4")
        try:
            def on_process(proc: asyncio.subprocess.Process) -> None:
                state.preview_proc = proc
                if state.preview_cancel.is_set() and proc.returncode is None:
                    proc.kill()

            await transcode_preview(
                Path(state.source_path),
                dest,
                has_audio=has_audio,
                cancel=state.preview_cancel,
                on_process=on_process,
            )
            state.preview_status = "ready"
        except asyncio.CancelledError:
            proc = state.preview_proc
            if proc is not None and proc.returncode is None:
                proc.kill()
            partial.unlink(missing_ok=True)
            raise
        except (FFmpegError, OSError) as exc:
            partial.unlink(missing_ok=True)
            if not state.preview_cancel.is_set():
                log.warning("preview transcode failed for %s: %s", state.id, exc)
            state.preview_status = "failed"
        finally:
            state.preview_proc = None
            self._publish(state)

    async def _stop_preview(self, state: JobState) -> None:
        state.preview_cancel.set()
        proc = state.preview_proc
        if proc is not None and proc.returncode is None:
            proc.kill()
        task = state.preview_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        _preview_locks.pop(state.id, None)

    def media_source(self, state: JobState) -> Path:
        """直接能播的返回原片，转好的返回预览副本。还在转或转失败时不把原片交给窗口。"""
        source = Path(state.source_path)
        preview = preview_file(state.id)
        if state.preview_status != "direct" and preview.is_file() and preview.stat().st_size > 0:
            return preview
        if state.preview_status in {"", "direct"}:
            return source
        raise FileNotFoundError("预览还没准备好")

    async def retry_failed_frames(self, job_id: str) -> JobState:
        state = await self.get(job_id)
        if state.status in {"queued", "running", "paused"}:
            raise BusyError("任务还在进行，结束后再重试失败的帧")
        if state.status not in {"done", "failed"}:
            raise BusyError("这个任务不能重试失败的帧")
        if state.failed_frames <= 0:
            raise BusyError("没有失败的帧")
        if not Path(state.source_path).is_file():
            raise FileNotFoundError("视频文件不存在")
        return await self._enqueue_retry(state, "frames", "排队重试失败的帧")

    async def retry_speech(self, job_id: str, part: str) -> JobState:
        if part not in {"transcribe", "judge"}:
            raise BusyError("只能重试口播转写或文字判定")
        state = await self.get(job_id)
        if state.status in {"queued", "running", "paused"}:
            raise BusyError("任务还在进行，结束后再重试口播")
        if state.status not in {"done", "failed"}:
            raise BusyError("这个任务不能重试口播")
        if not bool(state.options.get("asr_enabled", True)):
            raise BusyError("这次任务没有开口播检测")
        if _speech_unretryable(state):
            raise BusyError("口播已跳过，不能单独重试")
        if part == "judge":
            if not state.speech_transcript:
                raise BusyError("还没有口播文本，请先重试转写")
            if speech_retry_kind(state) != "judge":
                raise BusyError("文字判定已经完成")
            label = "排队重试文字判定"
        else:
            if state.speech_transcript and speech_retry_kind(state) == "judge":
                raise BusyError("转写已经完成，请只重试文字判定")
            if speech_retry_kind(state) != "transcribe":
                raise BusyError("口播转写没有失败")
            if not Path(state.source_path).is_file():
                raise FileNotFoundError("视频文件不存在")
            label = "排队重试口播转写"
        return await self._enqueue_retry(state, part, label)

    async def _enqueue_retry(self, state: JobState, part: str, label: str) -> JobState:
        async with self._gate:
            if state.status in {"queued", "running", "paused"}:
                raise BusyError("任务还在进行，结束后再重试")
            others = [
                job
                for job in self._jobs.values()
                if job.id != state.id and job.status in {"queued", "running", "paused"}
            ]
            if len(others) >= _MAX_QUEUE:
                raise BusyError("排队已满（最多 20 个），请等一部分完成后再重试")
            ahead = sum(1 for job in self._jobs.values() if job.status in {"queued", "running", "paused"})
            state.retry_part = part
            state.retry_failed = part == "frames"
            state.queue_at = utcnow()
            state.cancel_event = asyncio.Event()
            state.status = "queued"
            state.stage = "queued"
            state.finished_at = None
            state.error = None
            state.message = label if ahead == 0 else f"{label}，前面还有 {ahead} 个"
            if self._queue_paused and state.paused_at is None:
                state.paused_at = self._pause_mark or _precise_now()
            state.touch()
            await self.db.upsert_job(state.to_row())
            self._publish(state)
            self._kick_locked()
        return state

    def _kick_locked(self) -> None:
        if self._queue_paused:
            return
        if any(job.status in {"running", "paused"} for job in self._jobs.values()):
            return
        if any(not task.done() for task in self._tasks.values()):
            return
        queued = sorted(
            (
                job
                for job in self._jobs.values()
                if job.status == "queued" and not job.cancel_event.is_set()
            ),
            key=lambda job: (job.queue_at or job.created_at, job.id),
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

    @property
    def queue_paused(self) -> bool:
        return self._queue_paused

    async def pause_queue(self) -> bool:
        async with self._gate:
            self._queue_paused = True
            self._resume.clear()
            if self._pause_mark is None:
                self._pause_mark = _precise_now()
            mark = self._pause_mark
            running = [job for job in self._jobs.values() if job.status == "running"]
            holders = [job.holder for job in running if job.holder]
            touched: list[JobState] = []
            for state in self._jobs.values():
                if state.status in TERMINAL:
                    continue
                if state.paused_at is None:
                    state.paused_at = mark
                if state.status == "running":
                    state.status = "paused"
                    state.message = "已暂停"
                state.touch()
                touched.append(state)
            for holder in holders:
                _signal_processes(holder, getattr(signal, "SIGSTOP", None))
        for state in touched:
            await self.db.upsert_job(state.to_row())
            self._publish(state)
        await self._remember_queue_paused()
        return True

    async def resume_queue(self) -> bool:
        now = _precise_now()
        async with self._gate:
            self._queue_paused = False
            self._pause_mark = None
            touched: list[JobState] = []
            for state in self._jobs.values():
                if state.status in TERMINAL:
                    continue
                if state.paused_at:
                    _fold_pause(state, now)
                if state.status == "paused":
                    state.status = "running"
                    state.message = "继续识别"
                state.touch()
                touched.append(state)
            holders = [job.holder for job in touched if job.holder and job.status == "running"]
            for holder in holders:
                _signal_processes(holder, getattr(signal, "SIGCONT", None))
            self._resume.set()
            self._kick_locked()
        for state in touched:
            await self.db.upsert_job(state.to_row())
            self._publish(state)
        await self._remember_queue_paused()
        return False

    async def _remember_queue_paused(self) -> None:
        self._settings["queue_paused"] = self._queue_paused
        try:
            await self.db.save_settings(self._settings)
        except Exception:
            log.exception("保存队列暂停状态失败")

    async def clear_queue(self) -> int:
        """停掉并删除还在排队、正在识别、已暂停的视频。已结束的记录保留。"""
        rows = await self.db.list_jobs()
        ids = [row["id"] for row in rows if row["status"] in {"queued", "running", "paused"}]
        known = set(ids)
        for job_id, state in list(self._jobs.items()):
            if state.status in {"queued", "running", "paused"} and job_id not in known:
                ids.append(job_id)
        async with self._gate:
            was_paused = self._queue_paused
            self._queue_paused = True
            self._resume.clear()
        removed = 0
        try:
            for job_id in ids:
                try:
                    state = await self.get(job_id)
                except JobNotFound:
                    continue
                if state.status not in {"queued", "running", "paused"}:
                    continue
                await self.delete(job_id)
                removed += 1
        finally:
            async with self._gate:
                self._queue_paused = was_paused
                if was_paused:
                    self._resume.clear()
                else:
                    self._resume.set()
                    self._pause_mark = None
                    self._kick_locked()
        return removed

    async def delete_all(self) -> int:
        rows = await self.db.list_jobs()
        ids = [row["id"] for row in rows]
        extra = [job_id for job_id in list(self._jobs) if job_id not in set(ids)]
        for job_id in [*ids, *extra]:
            await self.delete(job_id)
        return len(ids) + len(extra)

    async def run(self, job_id: str) -> None:
        state = self._jobs[job_id]
        part = ""
        selective: list[int] = []
        kept_visual: list[Finding] = []
        recognized_base = 0
        async with self._gate:
            if self._queue_paused or state.cancel_event.is_set() or state.status in TERMINAL:
                return
            part = state.retry_part if state.retry_part in {"frames", "transcribe", "judge"} else ""
            if not part and state.retry_failed:
                part = "frames"
            state.retry_part = ""
            state.retry_failed = False
            state.hold_failed_indexes = False
            if part == "frames":
                selective = sorted({index for index in state.failed_indexes if isinstance(index, int) and index > 0})
                state.last_frame_error = ""
                state.error = None
                state.speech_hits = [finding_as_hit(item) for item in state.findings if item.source == "口播"]
                state.speech_ratio = 1.0
                if selective:
                    state.hold_failed_indexes = True
                    kept_visual = drop_retried_reviews(
                        [item for item in state.findings if item.source != "口播"],
                        selective,
                        state.fps,
                        state.duration_sec or 0,
                    )
                    recognized_base = max(0, state.frames_recognized - len(selective))
                    state.frames_recognized = recognized_base
                    state.visual_findings = list(kept_visual)
                else:
                    state.failed_frames = 0
                    state.failed_indexes = []
                    state.frames_recognized = 0
                    state.frames_extracted = 0
                    state.frames_estimated = 0
                    state.progress = 0
                    state.visual_findings = []
                state.findings = combine_findings(state.visual_findings, state.speech_hits)
                state.hit_count = len(state.findings)
                if selective:
                    self._apply_ratio(state)
            elif part in {"transcribe", "judge"}:
                state.error = None
                state.visual_findings = [item for item in state.findings if item.source != "口播"]
                state.speech_hits = []
                state.speech_ratio = 0.0
                state.asr_error = None
                state.speech_message = ""
                state.speech_judge = ""
                if part == "transcribe":
                    state.speech_stage = ""
                    state.speech_transcript = []
                else:
                    state.speech_stage = "judging"
                state.findings = list(state.visual_findings)
                state.hit_count = len(state.findings)
                self._apply_ratio(state)
            state.status = "running"
            if part == "judge":
                state.stage = "recognizing"
                state.message = "正在重试文字判定"
            elif part == "transcribe":
                state.stage = "extracting"
                state.message = "正在重试口播转写"
            elif selective:
                state.stage = "extracting"
                state.message = "正在重试失败的帧"
            elif part == "frames":
                state.stage = "extracting"
                state.message = "正在重试全部画面"
            else:
                state.stage = "extracting"
                state.message = "准备处理"
            state.started_at = state.started_at or utcnow()
            state.touch()
        await self.db.upsert_job(state.to_row())
        self._publish(state)
        holder: dict = {"proc": None, "audio_proc": None, "recognizer": None, "asr": None}
        state.holder = holder
        if part == "transcribe":
            _clear_speech_assignments(state.options)
        config = self._config_for(state)
        route = ModelRoute.bind(
            state.options,
            selected_model=config.model,
            allow_new_flash=not (part == "frames" and bool(selective)),
            flush=lambda: self._flush_route(state),
        )
        await self._flush_route(state)

        # 如果用户已登录、没开 mock 但 key 为空，自动补取一次。
        # 这处理登录时网络偶发导致 key 获取失败的情况。
        if (not config.mock and self._relay_logged_in() and not config.api_key):
            try:
                async with self._account_lock:
                    await self._ensure_relay_key()
                    await self.db.save_settings(self._settings)
                # 重新生成 config，用补取的 key
                config = self._config_for(state)
            except RelayError as exc:
                log.warning("start job %s: failed to fetch relay key: %s", state.id, exc)

        try:
            recognizer = build_recognizer(config, route)
            holder["recognizer"] = recognizer

            async def on_progress(progress: Progress) -> None:
                if selective:
                    progress.findings = refold_findings([*kept_visual, *progress.findings], config.fps)
                await self._on_visual(state, progress)

            async def on_speech(progress: SpeechProgress) -> None:
                await self._on_speech(state, progress)

            async with recognizer:
                still_failed: list[int] | None = None
                frame_error = ""
                speech_hits: list[RawHit] = []
                speech_error = ""
                if part == "judge":
                    speech_hits, speech_error = await self._judge_saved(state, recognizer, on_speech)
                    findings = list(state.visual_findings)
                elif part == "transcribe":
                    speech_task = self._start_speech(state, config, recognizer, holder, on_speech)
                    if speech_task is None:
                        speech_hits, speech_error = [], ""
                    else:
                        speech_hits, speech_error = await speech_task
                    findings = list(state.visual_findings)
                else:
                    if selective:
                        visual_task = asyncio.create_task(
                            run_selected_frames(
                                Path(state.source_path),
                                frames_dir(state.id),
                                config,
                                recognizer,
                                state.cancel_event,
                                on_progress,
                                lambda proc: holder.__setitem__("proc", proc),
                                selective,
                                recognized_base=recognized_base,
                                frames_estimated=state.frames_estimated,
                                frames_extracted=state.frames_extracted,
                                resume=self._resume,
                            ),
                            name=f"visual-{state.id}",
                        )
                    else:
                        visual_task = asyncio.create_task(
                            run_pipeline(
                                Path(state.source_path),
                                frames_dir(state.id),
                                config,
                                recognizer,
                                state.cancel_event,
                                on_progress,
                                lambda proc: holder.__setitem__("proc", proc),
                                self._resume,
                            ),
                            name=f"visual-{state.id}",
                        )
                    speech_task = None if part == "frames" else self._start_speech(state, config, recognizer, holder, on_speech)
                    try:
                        if selective:
                            findings, still_failed, frame_error = await visual_task
                            findings = refold_findings([*kept_visual, *findings], config.fps)
                        else:
                            findings = await visual_task
                    except BaseException:
                        state.cancel_event.set()
                        _kill_processes(holder)
                        if speech_task is not None:
                            speech_task.cancel()
                            await asyncio.gather(speech_task, return_exceptions=True)
                        raise
                    if speech_task is not None:
                        if not speech_task.done():
                            async with state.progress_lock:
                                if state.status not in TERMINAL:
                                    state.stage = "aggregating"
                                    state.message = "画面已完成，正在等待口播结果"
                                    state.touch()
                            self._publish(state)
                        speech_hits, speech_error = await speech_task
                    else:
                        speech_hits = list(state.speech_hits)
                if state.cancel_event.is_set():
                    raise JobCancelled()
            async with state.progress_lock:
                if still_failed is not None:
                    state.hold_failed_indexes = False
                    state.failed_indexes = list(still_failed)
                    state.failed_frames = len(still_failed)
                    state.last_frame_error = frame_error
                state.visual_findings = findings
                state.speech_hits = speech_hits
                if speech_error:
                    state.asr_error = speech_error
                    state.speech_stage = "failed"
                    state.speech_message = speech_error
                    if (
                        state.speech_transcript
                        and not _speech_unretryable(state)
                        and state.speech_judge not in {"partial", "ok"}
                    ):
                        state.speech_judge = "failed"
                state.findings = combine_findings(findings, speech_hits)
                state.hit_count = len(state.findings)
                state.status = "done"
                state.stage = "done"
                state.progress = 100
                state.error = None
                extra = _partial_frame_note(state.failed_frames, state.last_frame_error)
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
            stamp = utcnow()
            if state.status in TERMINAL:
                _fold_pause(state, stamp, terminal=True)
            state.finished_at = state.finished_at or stamp
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
        key, source = self._effective_key()
        secret_id, secret_key, _region, _secret_source = self._effective_tencent()
        options = state.options
        base_url = str(options.get("base_url") or self._settings["base_url"])
        model = configured_model(options.get("model") or self._settings["model"])
        if source == "account":
            base_url = CHAT_BASE
        return RunConfig(
            fps=float(options.get("fps", state.fps)),
            concurrency=clamp_concurrency(options.get("concurrency", state.concurrency)),
            max_width=int(options.get("max_width", 1280)),
            jpeg_q=int(options.get("jpeg_q", 5)),
            api_key=key,
            base_url=base_url,
            model=model,
            detail=str(options.get("detail") or "original"),
            thinking=bool(options.get("thinking", False)),
            motion_enabled=bool(options.get("motion_enabled", False)),
            timeout_sec=float(options.get("timeout_sec", 120)),
            mock=bool(options.get("mock", False)) or self._mock_enabled(),
            fault_bad_json=self._visible_fault("fault_bad_json"),
            fault_tencent=self._visible_fault("fault_tencent"),
            fault_partial_frames=self._visible_fault("fault_partial_frames"),
            asr_enabled=bool(options.get("asr_enabled", True)),
            tencent_secret_id=secret_id,
            tencent_secret_key=secret_key,
            tencent_region=FIXED_TENCENT_REGION,
            asr_engine=configured_engine(options.get("asr_engine")),
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

    def _env_tencent(self) -> tuple[str, str, str] | None:
        """未登录时的调试密钥。已登录则改向中转站要临时凭证，不读这里。"""
        secret_id = os.environ.get("TENCENTCLOUD_SECRET_ID", "").strip()
        secret_key = os.environ.get("TENCENTCLOUD_SECRET_KEY", "").strip()
        if not secret_id or not secret_key:
            return None
        region = os.environ.get("TENCENTCLOUD_REGION", "").strip() or FIXED_TENCENT_REGION
        return secret_id, secret_key, region

    def _start_speech(self, state: JobState, config: RunConfig, recognizer, holder: dict, on_speech):
        if not config.asr_enabled:
            return None
        if config.fault_tencent:
            asr = FaultAsrClient()
            prepare = None
        elif config.mock:
            asr = MockAsrClient()
            prepare = None
        elif self._relay_logged_in():
            asr = TencentAsrClient(
                "",
                "",
                config.tencent_region,
                config.asr_engine,
                timeout=max(30.0, config.timeout_sec),
                credential_provider=lambda: self._load_asr_credentials(state.cancel_event),
            )
            prepare = lambda client=asr: self._apply_relay_credentials(client, state.cancel_event)
        else:
            env = self._env_tencent()
            if env is not None:
                secret_id, secret_key, region = env
                asr = TencentAsrClient(
                    secret_id,
                    secret_key,
                    region,
                    config.asr_engine,
                    timeout=max(30.0, config.timeout_sec),
                )
                prepare = None
            else:
                message = "未登录，已跳过口播检测"

                async def skipped() -> tuple[list[RawHit], str]:
                    await on_speech(SpeechProgress(stage="skipped", message=message, hits=[], ratio=1))
                    return [], message

                return asyncio.create_task(skipped(), name=f"speech-{state.id}")
        holder["asr"] = asr
        return asyncio.create_task(
            self._speech_job(state, asr, recognizer, holder, on_speech, prepare),
            name=f"speech-{state.id}",
        )

    async def _load_asr_credentials(self, cancel: asyncio.Event | None = None) -> AsrCredentials:
        """向中转站要临时凭证。网络、5xx 和 429 最多再试 5 次。密钥不落盘、不写日志。"""
        token = str(self._settings.get("relay_access_token") or "").strip()
        if not token:
            raise RelayError("未登录，已跳过口播检测", 401)
        last: RelayError | None = None
        for attempt in range(ASR_MAX_RETRIES + 1):
            if attempt:
                await wait_backoff(retry_backoff_seconds(attempt), cancel, self._resume)
            try:
                data = await self.relay.asr_credentials(token)
            except RelayError as exc:
                last = exc
                if not _relay_credential_retryable(exc) or attempt >= ASR_MAX_RETRIES:
                    raise
                log.warning(
                    "asr credential fetch will retry %s/%s status=%s",
                    attempt + 1,
                    ASR_MAX_RETRIES,
                    exc.status,
                )
                continue
            try:
                return parse_relay_credentials(data)
            except ValueError:
                last = RelayError("中转站没有返回口播凭证", 502)
                if attempt >= ASR_MAX_RETRIES:
                    raise last
                log.warning(
                    "asr credential fetch will retry %s/%s status=502",
                    attempt + 1,
                    ASR_MAX_RETRIES,
                )
        assert last is not None
        raise last

    async def _apply_relay_credentials(self, asr: TencentAsrClient, cancel: asyncio.Event | None = None) -> None:
        creds = await self._load_asr_credentials(cancel)
        asr.apply_credentials(
            creds.secret_id,
            creds.secret_key,
            creds.region,
            creds.token,
            expired_at=creds.expired_at,
        )

    async def _remember_transcript(self, state: JobState, sentences) -> None:
        payload = dump_sentences(sentences)
        async with state.progress_lock:
            if state.status in TERMINAL:
                return
            state.speech_transcript = payload
            state.speech_judge = ""
            state.touch()
        try:
            await self.db.upsert_job(state.to_row())
        except Exception:
            log.exception("save speech transcript failed for %s", state.id)

    async def _judge_saved(self, state: JobState, recognizer, on_speech) -> tuple[list[RawHit], str]:
        sentences = load_sentences(state.speech_transcript)
        if not sentences:
            message = "没有可重试的口播文本"
            await on_speech(SpeechProgress(stage="failed", message=message, hits=[], ratio=1, judge="failed"))
            return [], message
        try:
            hits = await judge_transcript(sentences, recognizer, state.cancel_event, on_speech)
            return hits, ""
        except asyncio.CancelledError:
            raise
        except JobCancelled:
            return [], ""
        except Exception as exc:
            log.exception("speech judge %s failed", state.id)
            message = str(exc)[:500] or "口播检测失败"
            await on_speech(SpeechProgress(stage="failed", message=message, hits=[], ratio=1, judge="failed"))
            return [], message

    async def _speech_job(self, state, asr, recognizer, holder, on_speech, prepare=None) -> tuple[list[RawHit], str]:
        try:
            async with asr:

                async def on_transcript(sentences) -> None:
                    await self._remember_transcript(state, sentences)

                hits = await run_speech(
                    Path(state.source_path),
                    audio_dir(state.id),
                    asr,
                    recognizer,
                    state.cancel_event,
                    on_speech,
                    lambda proc: holder.__setitem__("audio_proc", proc),
                    resume=self._resume,
                    prepare=prepare,
                    on_transcript=on_transcript,
                )
            return hits, ""
        except asyncio.CancelledError:
            raise
        except JobCancelled:
            return [], ""
        except NoAudio as exc:
            await on_speech(SpeechProgress(stage="skipped", message=str(exc), hits=[], ratio=1))
            return [], str(exc)
        except RelayError as exc:
            message = str(exc)[:500] or "暂时取不到口播凭证"
            log.warning("speech %s skipped: %s", state.id, message)
            await on_speech(SpeechProgress(stage="failed", message=message, hits=[], ratio=1))
            return [], message
        except Exception as exc:
            log.exception("speech %s failed", state.id)
            message = str(exc)[:500] or "口播检测失败"
            judge = "failed" if state.speech_transcript else ""
            await on_speech(SpeechProgress(stage="failed", message=message, hits=[], ratio=1, judge=judge))
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
            if not state.hold_failed_indexes:
                state.failed_frames = progress.failed_frames
                state.failed_indexes = list(progress.failed_indexes)
                state.last_frame_error = progress.last_error
            state.visual_findings = progress.findings
            state.findings = combine_findings(progress.findings, state.speech_hits)
            state.hit_count = len(state.findings)
            self._apply_ratio(state)
            self._keep_paused(state)
            state.touch()
            self._publish(state)
            await self._maybe_save(state)

    async def _on_speech(self, state: JobState, progress: SpeechProgress) -> None:
        async with state.progress_lock:
            if state.status in TERMINAL:
                return
            state.speech_stage = progress.stage
            state.speech_message = progress.message
            if progress.judge in {"ok", "partial", "failed"}:
                state.speech_judge = progress.judge
            state.speech_hits = progress.hits
            state.speech_ratio = progress.ratio
            state.findings = combine_findings(state.visual_findings, progress.hits)
            state.hit_count = len(state.findings)
            self._apply_ratio(state)
            self._keep_paused(state)
            state.touch()
            self._publish(state)
            await self._maybe_save(state)

    def _keep_paused(self, state: JobState) -> None:
        if self._queue_paused and state.status in {"running", "paused"}:
            state.status = "paused"
            state.message = "已暂停"

    def _apply_ratio(self, state: JobState) -> None:
        denom = max(state.frames_estimated, 1)
        visual_ratio = state.frames_recognized / denom if state.frames_estimated else 0
        if state.options.get("asr_enabled", True):
            state.progress = min(99.0, (visual_ratio * 0.7 + state.speech_ratio * 0.3) * 100)
        else:
            state.progress = min(99.0, visual_ratio * 100)

    async def _flush_route(self, state: JobState) -> None:
        """闪帧和口播的新分配在发出请求前落盘，重启后仍用原来的模型。"""
        async with state.progress_lock:
            try:
                await self.db.upsert_job(state.to_row())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("save model route failed")

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
            _apply_refold(cached)
            await self.ensure_preview(cached)
            return cached
        row = await self.db.get_job(job_id)
        if row is None:
            raise JobNotFound(job_id)
        state = _state_from_row(row, await self.db.get_findings(job_id))
        self._jobs[job_id] = state
        _apply_refold(state)
        await self.ensure_preview(state)
        return state

    async def list_jobs(self) -> list[JobPublic]:
        rows = await self.db.list_jobs()
        result: list[JobPublic] = []
        for row in rows:
            state = self._jobs.get(row["id"])
            if state is None:
                findings = refold_findings(await self.db.get_findings(row["id"]), row["fps"])
                state = _state_from_row(row, [])
                if findings:
                    state.hit_count = len(findings)
            else:
                _apply_refold(state)
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
                    stamp = utcnow()
                    _fold_pause(state, stamp, terminal=True)
                    state.status = "cancelled"
                    state.stage = "cancelled"
                    state.message = "已取消"
                    state.finished_at = state.finished_at or stamp
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
        await self._stop_preview(state)
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

    async def export_batch(self, job_ids: list[str]) -> Path:
        ordered: list[str] = []
        seen: set[str] = set()
        for job_id in job_ids:
            if job_id in seen:
                continue
            seen.add(job_id)
            ordered.append(job_id)
        jobs = [await self.get(job_id) for job_id in ordered]
        ready = [job for job in jobs if job.status == "done"]
        if not ready:
            raise RuntimeError("这批视频还没有检测完成，不能导出")
        path = batch_export_file(len(ready))
        write_batch_xlsx(path, [
            BatchExportJob(
                filename=job.filename,
                duration_sec=job.duration_sec,
                fps=job.fps,
                status=job.status,
                findings=list(job.findings) if job.status == "done" else [],
            )
            for job in jobs
        ])
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
        usable = cuttable_findings(state.findings)
        if not usable:
            raise RuntimeError("没有命中区间，无需剪辑")
        probe = await probe_video(source)
        duration = probe.duration or state.duration_sec or 0
        silences = await detect_silences(source)
        cuts, keeps = draft_cuts(duration, usable, padding_sec, silences)
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

    async def account_options(self) -> AccountOptions:
        try:
            data = await self.relay.public_settings()
        except RelayError as exc:
            return AccountOptions(reachable=False, message=str(exc))
        return _options_from_public(data if isinstance(data, dict) else {})

    async def account_send_code(self, email: str) -> AccountAction:
        cleaned = _email_ok(email)
        async with self._account_lock:
            result = await self.relay.send_verify_code(cleaned)
        countdown = result.get("countdown") if isinstance(result, dict) else None
        message = "验证码已发送"
        if isinstance(countdown, int) and countdown > 0:
            message = f"验证码已发送，请等待 {countdown} 秒后再发"
        return AccountAction(settings=self.public_settings(), message=message)

    async def account_register(self, email: str, password: str, verify_code: str = "") -> AccountAction:
        cleaned = _email_ok(email)
        if len(password) < 6:
            raise RelayError("密码至少 6 位", 400)
        async with self._account_lock:
            data = await self.relay.register(cleaned, password, verify_code.strip())
            message = await self._accept_session(cleaned, data)
            return AccountAction(settings=self.public_settings(), message=message)

    async def account_login(self, email: str, password: str) -> AccountAction:
        cleaned = _email_ok(email)
        if not password:
            raise RelayError("请填写密码", 400)
        async with self._account_lock:
            data = await self.relay.login(cleaned, password)
            if data.get("requires_2fa"):
                temp = str(data.get("temp_token") or "").strip()
                if not temp:
                    raise RelayError("这个账号开启了两步验证，请重新登录", 502)
                self._pending_2fa = {"temp_token": temp, "email": cleaned}
                return AccountAction(
                    settings=self.public_settings(),
                    requires_2fa=True,
                    email_masked=str(data.get("user_email_masked") or ""),
                    message="请输入身份验证器中的 6 位数字",
                )
            message = await self._accept_session(cleaned, data)
            return AccountAction(settings=self.public_settings(), message=message)

    async def account_login_2fa(self, totp_code: str) -> AccountAction:
        code = totp_code.strip()
        if not re.fullmatch(r"\d{6}", code):
            raise RelayError("请输入 6 位数字验证码", 400)
        async with self._account_lock:
            pending = self._pending_2fa
            if not pending:
                raise RelayError("请先登录，再填写验证码", 400)
            data = await self.relay.login_2fa(pending["temp_token"], code)
            message = await self._accept_session(pending["email"], data)
            return AccountAction(settings=self.public_settings(), message=message)

    async def account_logout(self) -> AccountAction:
        async with self._account_lock:
            refresh = str(self._settings.get("relay_refresh_token") or "")
            if refresh:
                try:
                    await self.relay.logout(refresh)
                except RelayError:
                    pass
            await self._drop_session()
            return AccountAction(settings=self.public_settings(), message="已退出登录")

    async def account_refresh(self) -> AccountAction:
        async with self._account_lock:
            if not self._relay_logged_in():
                raise RelayError("还没有登录", 400)
            profile = await self._call_authed(self.relay.profile)
            self._apply_profile(profile if isinstance(profile, dict) else {})
            try:
                stats = await self._call_authed(self.relay.usage)
                self._apply_usage(stats if isinstance(stats, dict) else {})
            except RelayError:
                pass
            try:
                await self._ensure_relay_key()
            finally:
                await self.db.save_settings(self._settings)
            return AccountAction(settings=self.public_settings(), message="已更新余额")

    async def recharge_info(self) -> RechargeInfo:
        self.ensure_logged_in()
        async with self._account_lock:
            data = await self._call_authed(self.relay.checkout_info)
        return _recharge_info(data if isinstance(data, dict) else {})

    async def list_recharges(self) -> list[RechargeOrder]:
        self.ensure_logged_in()
        async with self._account_lock:
            rows = await self._call_authed(self.relay.list_balance_orders)
        if not isinstance(rows, list):
            return []
        return [_recharge_order(item) for item in rows[:20]]

    async def create_recharge(self, amount: float, payment_type: str) -> RechargeOrder:
        self.ensure_logged_in()
        kind = payment_type.strip()
        if not _PAYMENT_TYPE.fullmatch(kind):
            raise RelayError("请选择一种支付方式", 400)
        cents = round(float(amount) * 100)
        if cents <= 0 or cents > 100_000_000:
            raise RelayError("请填写大于 0 的充值金额", 400)
        money = cents / 100
        async with self._account_lock:
            info = await self._call_authed(self.relay.checkout_info)
            view = _recharge_info(info if isinstance(info, dict) else {})
            if view.balance_disabled:
                raise RelayError("余额充值已关闭", 403)
            chosen = next((item for item in view.methods if item.payment_type == kind), None)
            if chosen is None:
                raise RelayError("请选择页面上的一种支付方式", 400)
            _check_recharge_amount(money, view, chosen)
            created = await self._call_authed(lambda token: self.relay.create_balance_order(token, money, kind))
        if not isinstance(created, dict):
            created = {}
        order = _recharge_order(created)
        if order.order_id <= 0:
            raise RelayError("支付服务没有返回订单", 502)
        return order

    async def recharge_order(self, order_id: int) -> RechargeOrder:
        self.ensure_logged_in()
        if order_id <= 0:
            raise RelayError("订单不存在", 404)
        async with self._account_lock:
            data = await self._call_authed(lambda token: self.relay.get_payment_order(token, order_id))
            order = _recharge_order(data if isinstance(data, dict) else {})
            if order.order_id <= 0:
                raise RelayError("订单不存在", 404)
            if order.status in {"PAID", "RECHARGING", "COMPLETED"}:
                await self._refresh_account_numbers()
                order = order.model_copy(update={"settings": self.public_settings()})
            return order

    async def cancel_recharge(self, order_id: int) -> RechargeOrder:
        self.ensure_logged_in()
        if order_id <= 0:
            raise RelayError("订单不存在", 404)
        async with self._account_lock:
            await self._call_authed(lambda token: self.relay.cancel_payment_order(token, order_id))
            data = await self._call_authed(lambda token: self.relay.get_payment_order(token, order_id))
        order = _recharge_order(data if isinstance(data, dict) else {})
        if order.order_id <= 0:
            raise RelayError("订单不存在", 404)
        return order

    async def _refresh_account_numbers(self) -> None:
        profile = await self._call_authed(self.relay.profile)
        self._apply_profile(profile if isinstance(profile, dict) else {})
        try:
            stats = await self._call_authed(self.relay.usage)
            self._apply_usage(stats if isinstance(stats, dict) else {})
        except RelayError:
            pass
        await self.db.save_settings(self._settings)

    async def _accept_session(self, fallback_email: str, data: dict) -> str:
        if data.get("requires_2fa"):
            raise RelayError("这个账号开启了两步验证，请重新登录", 400)
        access = str(data.get("access_token") or "").strip()
        refresh = str(data.get("refresh_token") or "").strip()
        if not access:
            raise RelayError("中转站没有返回登录凭证", 502)
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        email = str(user.get("email") or fallback_email).strip() or fallback_email
        previous = str(self._settings.get("relay_email") or "")
        if previous and previous.lower() != email.lower():
            self._settings.pop("relay_api_key", None)
            self._settings.pop("relay_api_key_id", None)
            clear_image_session(self._settings)
        self._settings["relay_email"] = email
        self._settings["last_login_email"] = email
        self._settings["relay_login_at"] = time.time()
        self._settings["relay_access_token"] = access
        if refresh:
            self._settings["relay_refresh_token"] = refresh
        expires_in = _number(data.get("expires_in"))
        if expires_in and expires_in > 0:
            self._settings["relay_expires_at"] = time.time() + expires_in
        else:
            self._settings.pop("relay_expires_at", None)
        self._apply_profile(user)
        self._pending_2fa = None
        await self.db.save_settings(self._settings)
        try:
            await self._ensure_relay_key()
        except RelayError as exc:
            log.warning("picture key was not issued: %s", exc)
            await self.db.save_settings(self._settings)
            return f"已登录。{exc}"
        try:
            stats = await self._call_authed(self.relay.usage)
            self._apply_usage(stats if isinstance(stats, dict) else {})
        except RelayError:
            pass
        await self.db.save_settings(self._settings)
        return "已登录"

    def _apply_profile(self, profile: dict) -> None:
        if "balance" in profile:
            number = _number(profile.get("balance"))
            if number is not None:
                self._settings["relay_balance"] = number
        if "frozen_balance" in profile:
            number = _number(profile.get("frozen_balance"))
            if number is not None:
                self._settings["relay_frozen_balance"] = number
        email = str(profile.get("email") or "").strip()
        if email and "@" in email:
            self._settings["relay_email"] = email

    def _apply_usage(self, stats: dict) -> None:
        today = _number(stats.get("today_actual_cost"))
        total = _number(stats.get("total_actual_cost"))
        if today is not None:
            self._settings["relay_today_cost"] = today
        if total is not None:
            self._settings["relay_total_cost"] = total

    def _forget_local_relay_key(self) -> None:
        """丢掉本机记下的密钥指针。中转站上的密钥保留，不删除、不改分组。"""
        self._settings.pop("relay_api_key", None)
        self._settings.pop("relay_api_key_id", None)

    async def _resolve_relay_group_id(self) -> int:
        name = relay_group_name()
        fallback = relay_group_id_fallback()
        try:
            groups = await self._call_authed(self.relay.list_groups)
        except RelayError as exc:
            if exc.status == 401 or fallback is None:
                if exc.status == 401:
                    raise
                log.warning("relay group lookup failed: %s", groups_lookup_failed_message(name))
                raise RelayError("暂时无法确认账号权限，请稍后再试。", 502) from None
            return fallback
        if not isinstance(groups, list):
            groups = []
        return resolve_relay_group_id(groups, name=name, fallback_id=fallback)

    async def _ensure_relay_key(self) -> None:
        try:
            group_id = await self._resolve_relay_group_id()
        except RelayError as exc:
            if exc.status == 403:
                self._forget_local_relay_key()
            raise
        listed = await self._call_authed(self.relay.list_keys)
        if not isinstance(listed, list):
            listed = []
        chosen = self._pick_key(listed, group_id)
        if chosen is None:
            try:
                created = await self._call_authed(lambda token: self.relay.create_key(token, group_id))
            except RelayError:
                self._forget_local_relay_key()
                raise
            if not isinstance(created, dict):
                created = {}
            secret = str(created.get("key") or "")
            if not usable_api_key(secret):
                self._forget_local_relay_key()
                raise RelayError("中转站没有返回可用的调用密钥", 502)
            if created.get("ip_whitelist"):
                self._forget_local_relay_key()
                raise RelayError("新密钥带了 IP 限制，家庭网络会变动，不能用来识别", 502)
            created_id = created.get("id")
            if created_id is not None and not _same_id(created.get("group_id"), group_id):
                try:
                    updated = await self._call_authed(
                        lambda token: self.relay.update_key_group(token, int(created_id), group_id)
                    )
                except RelayError:
                    self._forget_local_relay_key()
                    raise
                if isinstance(updated, dict) and updated.get("group_id") is not None:
                    created["group_id"] = updated.get("group_id")
            if not _same_id(created.get("group_id"), group_id):
                self._forget_local_relay_key()
                log.warning("new relay key was not bound to group %s; existing keys were left unchanged", group_id)
                raise RelayError(KEY_NOT_READY, 502)
            self._settings["relay_api_key"] = secret.strip()
            if created_id is not None:
                self._settings["relay_api_key_id"] = created_id
            return
        self._settings["relay_api_key"] = chosen["key"]
        self._settings["relay_api_key_id"] = chosen["id"]

    def _pick_key(self, listed: list[dict], group_id: int) -> dict | None:
        """只选用已经绑在目标分组上的同名密钥。其他分组的密钥留在原处。"""
        local_key = str(self._settings.get("relay_api_key") or "").strip()
        local_id = self._settings.get("relay_api_key_id")
        first: dict | None = None
        for item in listed:
            if str(item.get("name") or "").strip() != KEY_NAME:
                continue
            if str(item.get("status") or "active") != "active":
                continue
            if item.get("ip_whitelist"):
                continue
            if not _same_id(item.get("group_id"), group_id):
                continue
            secret = str(item.get("key") or "")
            usable = usable_api_key(secret)
            local_match = usable_api_key(local_key) and _same_id(item.get("id"), local_id)
            if not usable and not local_match:
                continue
            record = {
                "key": secret.strip() if usable else local_key,
                "id": item.get("id"),
                "group_id": item.get("group_id"),
            }
            if local_match:
                return record
            if first is None and usable:
                first = record
        return first

    async def _token(self) -> str:
        if self._login_expired():
            await self._drop_session()
            raise RelayError("登录已过期，请重新登录", 401)
        token = str(self._settings.get("relay_access_token") or "").strip()
        if not token:
            raise RelayError("还没有登录", 400)
        expires = _number(self._settings.get("relay_expires_at"))
        if expires is not None and expires <= time.time() + 60:
            return await self._refresh_tokens()
        return token

    async def _call_authed(self, method):
        token = await self._token()
        try:
            return await method(token)
        except RelayError as exc:
            if exc.status != 401:
                raise
            token = await self._refresh_tokens()
            return await method(token)

    async def _refresh_tokens(self) -> str:
        refresh = str(self._settings.get("relay_refresh_token") or "").strip()
        if not refresh:
            await self._drop_session()
            raise RelayError("登录已过期，请重新登录", 401)
        try:
            pair = await self.relay.refresh(refresh)
        except RelayError:
            await self._drop_session()
            raise RelayError("登录已过期，请重新登录", 401) from None
        access = str(pair.get("access_token") or "").strip()
        if not access:
            await self._drop_session()
            raise RelayError("登录已过期，请重新登录", 401)
        self._settings["relay_access_token"] = access
        rotated = str(pair.get("refresh_token") or "").strip()
        if rotated:
            self._settings["relay_refresh_token"] = rotated
        expires_in = _number(pair.get("expires_in"))
        if expires_in and expires_in > 0:
            self._settings["relay_expires_at"] = time.time() + expires_in
        await self.db.save_settings(self._settings)
        return access

    async def _drop_session(self) -> None:
        for name in _RELAY_FIELDS:
            self._settings.pop(name, None)
        clear_image_session(self._settings)
        self._pending_2fa = None
        await self.db.save_settings(self._settings)

    async def list_image_groups(self) -> list[dict]:
        """当前登录用户可见的分组。只读，不创建密钥，也不改审核分组。"""
        self.ensure_logged_in()
        async with self._account_lock:
            data = await self._call_authed(self.relay.list_groups)
        return data if isinstance(data, list) else []

    async def ensure_image_key(self, group_id: int) -> str:
        """准备绑在生图分组上的用户密钥。不读取、不修改审核密钥。"""
        self.ensure_logged_in()
        async with self._account_lock:
            listed = await self._call_authed(self.relay.list_keys)
            if not isinstance(listed, list):
                listed = []
            chosen = self._pick_image_key(listed, group_id)
            if chosen is None:
                try:
                    created = await self._call_authed(
                        lambda token: self.relay.create_key_named(token, IMAGE_KEY_NAME, group_id)
                    )
                except RelayError:
                    clear_image_session(self._settings)
                    await self.db.save_settings(self._settings)
                    raise
                if not isinstance(created, dict):
                    created = {}
                secret = str(created.get("key") or "")
                if not usable_api_key(secret) or created.get("ip_whitelist"):
                    clear_image_session(self._settings)
                    await self.db.save_settings(self._settings)
                    raise RelayError("中转站没有返回可用的生图密钥", 502)
                created_id = created.get("id")
                if created_id is not None and not _same_id(created.get("group_id"), group_id):
                    updated = await self._call_authed(
                        lambda token: self.relay.update_key_group(token, int(created_id), group_id)
                    )
                    if isinstance(updated, dict) and updated.get("group_id") is not None:
                        created["group_id"] = updated.get("group_id")
                if not _same_id(created.get("group_id"), group_id):
                    clear_image_session(self._settings)
                    await self.db.save_settings(self._settings)
                    raise RelayError("生图密钥没有绑到专用分组", 502)
                self._settings["image_relay_api_key"] = secret.strip()
                self._settings["image_relay_api_key_id"] = created_id
                self._settings["image_relay_group_id"] = group_id
                await self.db.save_settings(self._settings)
                return secret.strip()
            self._settings["image_relay_api_key"] = chosen["key"]
            self._settings["image_relay_api_key_id"] = chosen["id"]
            self._settings["image_relay_group_id"] = group_id
            await self.db.save_settings(self._settings)
            return chosen["key"]

    def _pick_image_key(self, listed: list[dict], group_id: int) -> dict | None:
        local_key = str(self._settings.get("image_relay_api_key") or "").strip()
        local_id = self._settings.get("image_relay_api_key_id")
        first: dict | None = None
        for item in listed:
            if str(item.get("name") or "").strip() != IMAGE_KEY_NAME:
                continue
            if str(item.get("status") or "active") != "active":
                continue
            if item.get("ip_whitelist"):
                continue
            if not _same_id(item.get("group_id"), group_id):
                continue
            secret = str(item.get("key") or "")
            usable = usable_api_key(secret)
            local_match = usable_api_key(local_key) and _same_id(item.get("id"), local_id)
            if not usable and not local_match:
                continue
            record = {"key": secret.strip() if usable else local_key, "id": item.get("id")}
            if local_match:
                return record
            if first is None and usable:
                first = record
        return first


def _same_id(left: object, right: object) -> bool:
    if left is None or right is None:
        return False
    return str(left) == str(right)


def _options_from_public(data: dict) -> AccountOptions:
    if data.get("turnstile_enabled"):
        captcha = "turnstile"
    elif data.get("tencent_captcha_enabled"):
        captcha = "tencent"
    elif data.get("aliyun_captcha_enabled"):
        captcha = "aliyun"
    else:
        captcha = "none"
    notes: list[str] = []
    if data.get("backend_mode_enabled"):
        notes.append("中转站处于后台模式，普通用户不能登录。")
    if captcha != "none":
        notes.append("站点开启了人机验证。请先在中转站关闭验证码，再在这里登录。")
    return AccountOptions(
        reachable=True,
        registration_enabled=bool(data.get("registration_enabled", True)),
        email_verify_enabled=bool(data.get("email_verify_enabled")),
        captcha=captcha,
        message="".join(notes),
    )


_PAYMENT_TYPE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_ORDER_STATUS = re.compile(r"^[A-Z_]{1,40}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_QR_PREFIXES = ("https://", "http://", "weixin://", "wxp://", "alipay://", "alipays://")
_METHOD_LABELS = {
    "alipay": "支付宝",
    "alipay_direct": "支付宝",
    "wxpay": "微信支付",
    "wxpay_direct": "微信支付",
    "stripe": "银行卡",
    "easypay": "易支付",
    "airwallex": "Airwallex",
}


def _money(value: object, cap: float = 1_000_000_000) -> float:
    number = _number(value)
    if number is None or number < 0 or number > cap:
        return 0
    return round(number, 4)


def _plain(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = redact(value.strip())
    if "***" in text or len(text) > limit:
        return ""
    return text


def _http_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    lowered = text.lower()
    if len(text) > 2000 or not (lowered.startswith("https://") or lowered.startswith("http://")):
        return ""
    return text


def _qr_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    lowered = text.lower()
    if len(text) > 2000 or not lowered.startswith(_QR_PREFIXES):
        return ""
    return text


def _stamp(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not re.fullmatch(r"[0-9T:\-+.Zz ]{1,40}", text):
        return ""
    return text


def _pay_targets(data: dict) -> tuple[str, str, str]:
    pay_url = _http_url(data.get("pay_url"))
    qr_code = _qr_text(data.get("qr_code"))
    if not qr_code:
        raw_pay = data.get("pay_url")
        if isinstance(raw_pay, str) and raw_pay.strip() and not pay_url:
            qr_code = _qr_text(raw_pay)
    if pay_url or qr_code:
        return pay_url, qr_code, ""
    result_type = str(data.get("result_type") or "")
    if result_type in {"oauth_required", "jsapi_ready"}:
        return "", "", "这个方式要在微信里打开网页才能付款。请换一个支持扫码的方式。"
    if result_type == "order_created" or "client_secret" in data or "pay_url" in data or "qr_code" in data:
        return "", "", "支付服务没有返回可打开的支付链接或二维码。请换一种方式，或稍后再试。"
    return "", "", ""


def _unit(currency: str) -> str:
    if currency in {"", "CNY"}:
        return "元"
    return currency


def _check_recharge_amount(money: float, view: RechargeInfo, method: RechargeMethod) -> None:
    unit = _unit(method.currency or "")
    if method.single_min > 0 and money < method.single_min:
        raise RelayError(f"单笔最少 {method.single_min:g} {unit}", 400)
    if method.single_max > 0 and money > method.single_max:
        raise RelayError(f"单笔最多 {method.single_max:g} {unit}", 400)
    if view.global_min > 0 and money < view.global_min:
        raise RelayError(f"单笔最少 {view.global_min:g} {unit}", 400)
    if view.global_max > 0 and money > view.global_max:
        raise RelayError(f"单笔最多 {view.global_max:g} {unit}", 400)


def _recharge_info(data: dict) -> RechargeInfo:
    methods: list[RechargeMethod] = []
    raw_methods = data.get("methods")
    if isinstance(raw_methods, dict):
        for key, value in raw_methods.items():
            kind = str(key).strip()
            if not _PAYMENT_TYPE.fullmatch(kind):
                continue
            item = value if isinstance(value, dict) else {}
            label = str(item.get("display_name") or item.get("displayName") or "").strip()
            if not label:
                label = _METHOD_LABELS.get(kind, kind)
            currency = str(item.get("currency") or "").strip().upper()
            if not _CURRENCY.fullmatch(currency):
                currency = ""
            methods.append(
                RechargeMethod(
                    payment_type=kind,
                    display_name=label[:40],
                    currency=currency,
                    fee_rate=min(_money(item.get("fee_rate"), 100), 100),
                    daily_limit=_money(item.get("daily_limit")),
                    single_min=_money(item.get("single_min")),
                    single_max=_money(item.get("single_max")),
                )
            )
    disabled = bool(data.get("balance_disabled"))
    multiplier = _number(data.get("balance_recharge_multiplier"))
    if multiplier is None or multiplier <= 0:
        multiplier = 1
    if disabled:
        message = "余额充值已关闭"
    elif not methods:
        message = "暂时没有可用的支付方式。"
    else:
        message = ""
    return RechargeInfo(
        balance_disabled=disabled,
        global_min=_money(data.get("global_min")),
        global_max=_money(data.get("global_max")),
        recharge_fee_rate=min(_money(data.get("recharge_fee_rate"), 100), 100),
        balance_recharge_multiplier=round(multiplier, 4),
        # 本产品不提供充值优惠。中转站的阶梯和活动文案不往页面传。
        bonus_mode="bonus",
        bonus_notice="",
        bonus_tiers=[],
        help_text=_plain(data.get("help_text"), 2000),
        methods=methods,
        message=message,
    )


def _recharge_order(data: dict) -> RechargeOrder:
    raw_id = data.get("order_id", data.get("id"))
    try:
        order_id = int(raw_id)
    except (TypeError, ValueError):
        order_id = 0
    if order_id <= 0 or order_id > 2_000_000_000:
        order_id = 0
    status = str(data.get("status") or "").strip().upper()
    if not _ORDER_STATUS.fullmatch(status):
        status = ""
    kind = str(data.get("payment_type") or "").strip()
    if not _PAYMENT_TYPE.fullmatch(kind):
        kind = ""
    result_type = str(data.get("result_type") or "").strip()
    if result_type not in {"", "order_created", "oauth_required", "jsapi_ready"}:
        result_type = ""
    currency = str(data.get("currency") or "").strip().upper()
    if not _CURRENCY.fullmatch(currency):
        currency = ""
    pay_url, qr_code, message = _pay_targets(data)
    return RechargeOrder(
        order_id=order_id,
        amount=_money(data.get("amount")),
        pay_amount=_money(data.get("pay_amount")),
        fee_rate=min(_money(data.get("fee_rate"), 100), 100),
        bonus_amount=_money(data.get("bonus_amount")),
        status=status,
        result_type=result_type,
        payment_type=kind,
        pay_url=pay_url,
        qr_code=qr_code,
        currency=currency,
        expires_at=_stamp(data.get("expires_at")),
        created_at=_stamp(data.get("created_at")),
        message=message,
    )


def finding_as_hit(item: Finding) -> RawHit:
    return RawHit(
        start_sec=item.start_sec,
        end_sec=item.end_sec,
        category=item.category,
        text=item.text,
        position=item.position,
        confidence=item.confidence,
        reason=item.reason,
        note=item.note,
        source=item.source or "口播",
        sentence_start=item.sentence_start,
        sentence_end=item.sentence_end,
        next_start=item.next_start,
    )


def combine_findings(visual: list[Finding], speech_hits: list[RawHit]) -> list[Finding]:
    merged = list(visual) + aggregate(speech_hits)
    merged.sort(key=lambda item: (item.start_sec, item.source, item.category, item.text))
    return merged


def _strip_ready(directory: Path) -> bool:
    if not directory.is_dir():
        return False
    return any(path.is_file() and path.stat().st_size > 0 for path in directory.glob("strip_*.jpg"))


def _signal_processes(holder: dict, sig: int | None) -> None:
    """暂停时停住抽帧和抽音频。Windows 没有 SIGSTOP，只靠识别循环里等待。"""
    if sig is None:
        return
    for name in ("proc", "audio_proc"):
        proc = holder.get(name)
        if proc is None or proc.returncode is not None:
            continue
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass


def _kill_processes(holder: dict) -> None:
    _signal_processes(holder, getattr(signal, "SIGCONT", None))
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
