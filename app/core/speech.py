"""从视频抽音频，做录音识别，再用与画面相同的规则判定口播。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.core.deepseek import SpeechJudgeError
from app.core.ffmpeg import FFmpegError, extract_audio, probe_video, speech_codec
from app.core.pipeline import JobCancelled, cleanup_tree, wait_while_paused
from app.core.tencent_asr import MAX_AUDIO_BYTES, MockAsrClient, TencentAsrClient, chunk_spans
from app.core.transcript import SpeechSentence
from app.schemas import RawHit


class NoAudio(Exception):
    pass


class SpeechProgress:
    def __init__(self, *, stage: str, message: str, hits: list[RawHit], ratio: float, judge: str = ""):
        self.stage = stage
        self.message = message
        self.hits = hits
        self.ratio = ratio
        self.judge = judge


SpeechCb = Callable[[SpeechProgress], Awaitable[None]]
ProcessCb = Callable[[asyncio.subprocess.Process | None], None]
PrepareCb = Callable[[], Awaitable[None]]
TranscriptCb = Callable[[list[SpeechSentence]], Awaitable[None]]


async def run_speech(
    video: Path,
    work_dir: Path,
    asr: TencentAsrClient | MockAsrClient,
    judge,
    cancel: asyncio.Event,
    on_progress: SpeechCb,
    on_process: ProcessCb | None = None,
    resume: asyncio.Event | None = None,
    prepare: PrepareCb | None = None,
    on_transcript: TranscriptCb | None = None,
) -> list[RawHit]:
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        return await _run(
            video, work_dir, asr, judge, cancel, on_progress, on_process, resume, prepare, on_transcript
        )
    finally:
        cleanup_tree(work_dir)


async def _run(
    video: Path,
    work_dir: Path,
    asr: TencentAsrClient | MockAsrClient,
    judge,
    cancel: asyncio.Event,
    on_progress: SpeechCb,
    on_process: ProcessCb | None,
    resume: asyncio.Event | None = None,
    prepare: PrepareCb | None = None,
    on_transcript: TranscriptCb | None = None,
) -> list[RawHit]:
    prepared = asyncio.create_task(prepare()) if prepare is not None else None
    try:
        return await _recognize(
            video,
            work_dir,
            asr,
            judge,
            cancel,
            on_progress,
            on_process,
            resume,
            prepared,
            on_transcript,
        )
    finally:
        if prepared is not None:
            if not prepared.done():
                prepared.cancel()
            try:
                await prepared
            except (asyncio.CancelledError, Exception):
                pass


async def _recognize(
    video: Path,
    work_dir: Path,
    asr: TencentAsrClient | MockAsrClient,
    judge,
    cancel: asyncio.Event,
    on_progress: SpeechCb,
    on_process: ProcessCb | None,
    resume: asyncio.Event | None,
    prepared: asyncio.Task | None,
    on_transcript: TranscriptCb | None = None,
) -> list[RawHit]:
    await on_progress(SpeechProgress(stage="audio", message="正在提取音频", hits=[], ratio=0.15))
    probe = await probe_video(video)
    if not probe.has_audio:
        raise NoAudio("视频没有音轨，已跳过口播检测")
    pieces = await _pieces(video, work_dir, probe.duration, cancel, on_process)
    if cancel.is_set():
        raise JobCancelled()
    await wait_while_paused(cancel, resume)
    if cancel.is_set():
        raise JobCancelled()
    if prepared is not None:
        await prepared
    if cancel.is_set():
        raise JobCancelled()
    await on_progress(SpeechProgress(stage="submitting", message="正在提交腾讯云识别", hits=[], ratio=0.35))

    async def on_status(message: str) -> None:
        stage = "submitting" if "提交" in message else "transcribing"
        ratio = 0.4 if stage == "submitting" else 0.7
        await on_progress(SpeechProgress(stage=stage, message=message, hits=[], ratio=ratio))

    sentences = await asr.transcribe(pieces, cancel=cancel, on_status=on_status, resume=resume)
    sentences = [item for item in sentences if item.text.strip()]
    if cancel.is_set():
        raise JobCancelled()
    await wait_while_paused(cancel, resume)
    if cancel.is_set():
        raise JobCancelled()
    if not sentences:
        await on_progress(SpeechProgress(stage="done", message="没有识别到口播文本", hits=[], ratio=1))
        return []
    if on_transcript is not None:
        await on_transcript(sentences)
    if cancel.is_set():
        raise JobCancelled()
    return await judge_transcript(sentences, judge, cancel, on_progress)


async def judge_transcript(
    sentences: list[SpeechSentence],
    judge,
    cancel: asyncio.Event,
    on_progress: SpeechCb,
) -> list[RawHit]:
    """只对已经转好的文字做判定。不再抽音频，也不再请求腾讯云。"""
    await on_progress(
        SpeechProgress(stage="judging", message=f"正在判定口播文本（{len(sentences)} 句）", hits=[], ratio=0.85)
    )
    try:
        hits = await judge.judge_speech(sentences)
        message = f"口播判定完成，命中 {len(hits)} 条"
        judge_status = "ok"
    except SpeechJudgeError as exc:
        hits = list(exc.hits)
        message = str(exc)
        judge_status = "partial"
    if cancel.is_set():
        raise JobCancelled()
    await on_progress(SpeechProgress(stage="done", message=message, hits=hits, ratio=1, judge=judge_status))
    return hits


async def _pieces(
    video: Path,
    work_dir: Path,
    duration: float,
    cancel: asyncio.Event,
    on_process: ProcessCb | None,
) -> list[tuple[float, Path]]:
    suffix, codec = await speech_codec()
    whole = work_dir / f"speech{suffix}"
    await extract_audio(video, whole, codec_args=codec, cancel=cancel, on_process=on_process)
    if cancel.is_set():
        raise JobCancelled()
    size = whole.stat().st_size if whole.is_file() else 0
    if size <= MAX_AUDIO_BYTES or duration <= 0:
        return [(0.0, whole)]
    spans = chunk_spans(duration, size, MAX_AUDIO_BYTES)
    whole.unlink(missing_ok=True)
    pieces: list[tuple[float, Path]] = []
    for index, (start, end) in enumerate(spans):
        dest = work_dir / f"speech_{index:04d}{suffix}"
        pieces.extend(
            await _bounded(
                video,
                dest,
                start,
                max(0.1, end - start),
                suffix,
                codec,
                cancel,
                on_process,
            )
        )
    if not pieces:
        raise FFmpegError("没有提取到音频")
    return pieces


async def _bounded(
    video: Path,
    dest: Path,
    start: float,
    duration: float,
    suffix: str,
    codec: list[str],
    cancel: asyncio.Event,
    on_process: ProcessCb | None,
) -> list[tuple[float, Path]]:
    await extract_audio(
        video,
        dest,
        codec_args=codec,
        cancel=cancel,
        on_process=on_process,
        start=start,
        duration=duration,
    )
    if cancel.is_set():
        raise JobCancelled()
    size = dest.stat().st_size if dest.is_file() else 0
    if size <= MAX_AUDIO_BYTES or duration <= 8:
        if size > 5 * 1024 * 1024:
            raise FFmpegError("音频片段仍超过腾讯云 5MB 限制")
        return [(start, dest)]
    dest.unlink(missing_ok=True)
    half = duration / 2
    left = dest.with_name(dest.stem + "a" + suffix)
    right = dest.with_name(dest.stem + "b" + suffix)
    pieces = await _bounded(video, left, start, half, suffix, codec, cancel, on_process)
    pieces.extend(await _bounded(video, right, start + half, duration - half, suffix, codec, cancel, on_process))
    return pieces
