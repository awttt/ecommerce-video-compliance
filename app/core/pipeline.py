"""边抽帧边识别。抽帧速度受识别队列长度限制，避免中间图片堆满磁盘。"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.core.aggregate import aggregate
from app.core.deepseek import Recognizer
from app.core.ffmpeg import FFmpegError, extract_frames, extract_one_frame, frame_bounds, probe_video
from app.schemas import MANUAL_REVIEW_CATEGORY, MANUAL_REVIEW_NOTE, Finding, Frame, RawHit, RunConfig, is_manual_review

log = logging.getLogger(__name__)


class JobCancelled(Exception):
    pass


def manual_review_hit(start_sec: float, end_sec: float, reason: str) -> RawHit:
    """识别失败的一帧。时间用这一帧的起止，备注留给导出后人工看。"""
    compact = " ".join(str(reason or "").split())
    if len(compact) > 120:
        compact = compact[:120]
    return RawHit(
        start_sec=start_sec,
        end_sec=end_sec,
        category=MANUAL_REVIEW_CATEGORY,
        text="",
        position="",
        confidence="",
        reason=compact or "画面识别失败",
        note=MANUAL_REVIEW_NOTE,
        source="画面",
    )


def drop_retried_reviews(
    findings: list[Finding],
    indexes: list[int],
    fps: float,
    duration: float,
) -> list[Finding]:
    """重试某一帧时拿掉这一帧原来的解析失败行。重试再失败会重新记一条。"""
    windows: list[tuple[float, float]] = []
    for index in indexes:
        bounds = frame_bounds(int(index), fps, duration)
        if bounds is not None:
            windows.append(bounds)
    if not windows:
        return list(findings)
    kept: list[Finding] = []
    for item in findings:
        if is_manual_review(item.category) and any(
            abs(item.start_sec - start) < 0.05 and abs(item.end_sec - end) < 0.05
            for start, end in windows
        ):
            continue
        kept.append(item)
    return kept


def recognition_max_edge(max_width: int, detail: str) -> int:
    """低细节只在本地缩小长边。模型图片对象不接受 detail。"""
    edge = max(1, int(max_width))
    if detail == "low":
        return min(edge, 640)
    return edge


class Progress:
    def __init__(
        self,
        *,
        stage: str,
        message: str,
        duration_sec: float,
        frames_estimated: int,
        frames_extracted: int,
        frames_recognized: int,
        failed_frames: int,
        findings: list[Finding],
        last_error: str = "",
        failed_indexes: list[int] | None = None,
    ):
        self.stage = stage
        self.message = message
        self.duration_sec = duration_sec
        self.frames_estimated = frames_estimated
        self.frames_extracted = frames_extracted
        self.frames_recognized = frames_recognized
        self.failed_frames = failed_frames
        self.findings = findings
        self.last_error = last_error
        self.failed_indexes = list(failed_indexes or [])


ProgressCb = Callable[[Progress], Awaitable[None]]
ProcessCb = Callable[[asyncio.subprocess.Process | None], None]


def cleanup_tree(path: Path) -> None:
    if os.environ.get("EVC_KEEP_FRAMES") == "1":
        return
    shutil.rmtree(path, ignore_errors=True)


async def wait_while_paused(cancel: asyncio.Event, resume: asyncio.Event | None) -> None:
    """resume 被设置时可以继续。取消后马上返回，由调用方再看 cancel。"""
    if resume is None or resume.is_set():
        return
    while not resume.is_set():
        if cancel.is_set():
            return
        try:
            await asyncio.wait_for(resume.wait(), timeout=0.25)
        except TimeoutError:
            continue


async def run_pipeline(
    video: Path,
    work_dir: Path,
    config: RunConfig,
    recognizer: Recognizer,
    cancel: asyncio.Event,
    on_progress: ProgressCb,
    on_process: ProcessCb | None = None,
    resume: asyncio.Event | None = None,
) -> list[Finding]:
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        return await _run(video, work_dir, config, recognizer, cancel, on_progress, on_process, resume)
    finally:
        cleanup_tree(work_dir)


async def _run(
    video: Path,
    work_dir: Path,
    config: RunConfig,
    recognizer: Recognizer,
    cancel: asyncio.Event,
    on_progress: ProgressCb,
    on_process: ProcessCb | None,
    resume: asyncio.Event | None = None,
) -> list[Finding]:
    probe = await probe_video(video)
    if not probe.has_video or probe.duration <= 0:
        raise FFmpegError("无法读取视频画面或时长")
    estimated = max(1, math.ceil(probe.duration * config.fps - 1e-6))
    extracted = 0
    recognized = 0
    failed = 0
    failed_indexes: list[int] = []
    last_error = ""
    raw: list[RawHit] = []
    lock = asyncio.Lock()

    async def publish(stage: str, message: str) -> None:
        async with lock:
            snapshot = list(raw)
            state = Progress(
                stage=stage,
                message=message,
                duration_sec=probe.duration,
                frames_estimated=max(estimated, extracted),
                frames_extracted=extracted,
                frames_recognized=recognized,
                failed_frames=failed,
                findings=aggregate(snapshot),
                last_error=last_error,
                failed_indexes=list(failed_indexes),
            )
        await on_progress(state)

    await publish("extracting", "正在抽帧")
    queue: asyncio.Queue[Frame | None] = asyncio.Queue(maxsize=max(1, config.concurrency) * 2)

    async def worker() -> None:
        nonlocal recognized, failed, last_error
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                await wait_while_paused(cancel, resume)
                if cancel.is_set():
                    continue
                try:
                    hits = await recognizer.recognize(item)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    async with lock:
                        failed += 1
                        recognized += 1
                        failed_indexes.append(item.index)
                        last_error = str(exc)
                        raw.append(manual_review_hit(item.start_sec, item.end_sec, str(exc)))
                    log.warning("frame %s failed: %s", item.index, exc)
                else:
                    async with lock:
                        recognized += 1
                        raw.extend(hits)
                finally:
                    try:
                        Path(item.path).unlink(missing_ok=True)
                    except OSError:
                        log.debug("failed to remove frame %s", item.path)
                async with lock:
                    done = recognized
                    bad = failed
                    seen = extracted
                await publish(
                    "recognizing",
                    f"已抽帧 {seen} 帧，已识别 {done}/{max(estimated, seen)} 帧"
                    + (f"，失败 {bad} 帧" if bad else ""),
                )
            finally:
                queue.task_done()

    workers = [
        asyncio.create_task(worker(), name=f"recognize-{index}")
        for index in range(max(1, config.concurrency))
    ]

    async def stop_workers_on_cancel() -> None:
        await cancel.wait()
        for task in workers:
            if not task.done():
                task.cancel()

    watcher = asyncio.create_task(stop_workers_on_cancel())
    try:
        try:
            async for frame in extract_frames(
                video,
                work_dir,
                fps=config.fps,
                max_edge=recognition_max_edge(config.max_width, config.detail),
                jpeg_q=config.jpeg_q,
                duration=probe.duration,
                cancel=cancel,
                on_process=on_process,
            ):
                await wait_while_paused(cancel, resume)
                if cancel.is_set():
                    break
                async with lock:
                    extracted += 1
                    seen = extracted
                    done = recognized
                await publish(
                    "extracting" if done == 0 else "recognizing",
                    f"已抽帧 {seen} 帧，已识别 {done}/{max(estimated, seen)} 帧",
                )
                while not cancel.is_set():
                    try:
                        await asyncio.wait_for(queue.put(frame), timeout=0.3)
                        break
                    except TimeoutError:
                        continue
            if cancel.is_set():
                raise JobCancelled()
            for _ in workers:
                while not cancel.is_set():
                    try:
                        await asyncio.wait_for(queue.put(None), timeout=0.3)
                        break
                    except TimeoutError:
                        continue
                else:
                    raise JobCancelled()
            await asyncio.gather(*workers)
        except asyncio.CancelledError:
            if cancel.is_set():
                raise JobCancelled() from None
            raise
    finally:
        watcher.cancel()
        for task in workers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await asyncio.gather(watcher, return_exceptions=True)

    if cancel.is_set():
        raise JobCancelled()
    if extracted == 0:
        raise FFmpegError("没有抽出任何帧，请确认视频编码可以被 FFmpeg 读取")
    if recognized and failed == recognized:
        detail = last_error or "没有成功的识别结果"
        raise RuntimeError(f"全部分帧识别失败（{failed} 帧）。{detail}")

    findings = aggregate(raw)
    message = f"正在汇总时间区间，当前 {len(findings)} 条"
    await publish("aggregating", message)
    return findings


async def run_selected_frames(
    video: Path,
    work_dir: Path,
    config: RunConfig,
    recognizer: Recognizer,
    cancel: asyncio.Event,
    on_progress: ProgressCb,
    on_process: ProcessCb | None,
    indexes: list[int],
    *,
    recognized_base: int = 0,
    frames_estimated: int = 0,
    frames_extracted: int = 0,
    resume: asyncio.Event | None = None,
) -> tuple[list[Finding], list[int], str]:
    """只重抽并识别列出的帧号。返回新命中、仍失败的帧号和最后一次错误。"""
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        return await _run_selected(
            video,
            work_dir,
            config,
            recognizer,
            cancel,
            on_progress,
            on_process,
            indexes,
            recognized_base=recognized_base,
            frames_estimated=frames_estimated,
            frames_extracted=frames_extracted,
            resume=resume,
        )
    finally:
        cleanup_tree(work_dir)


async def _run_selected(
    video: Path,
    work_dir: Path,
    config: RunConfig,
    recognizer: Recognizer,
    cancel: asyncio.Event,
    on_progress: ProgressCb,
    on_process: ProcessCb | None,
    indexes: list[int],
    *,
    recognized_base: int,
    frames_estimated: int,
    frames_extracted: int,
    resume: asyncio.Event | None,
) -> tuple[list[Finding], list[int], str]:
    probe = await probe_video(video)
    if not probe.has_video or probe.duration <= 0:
        raise FFmpegError("无法读取视频画面或时长")
    chosen = sorted({index for index in indexes if index > 0})
    estimated = frames_estimated or max(chosen or [1])
    extracted = frames_extracted or estimated
    recognized = recognized_base
    failed = 0
    failed_indexes: list[int] = []
    last_error = ""
    raw: list[RawHit] = []
    lock = asyncio.Lock()
    total = len(chosen)

    async def publish(stage: str, message: str) -> None:
        async with lock:
            state = Progress(
                stage=stage,
                message=message,
                duration_sec=probe.duration,
                frames_estimated=estimated,
                frames_extracted=extracted,
                frames_recognized=recognized,
                failed_frames=failed,
                findings=aggregate(list(raw)),
                last_error=last_error,
                failed_indexes=list(failed_indexes),
            )
        await on_progress(state)

    await publish("recognizing", f"正在重试 {total} 帧")
    queue: asyncio.Queue[Frame | None] = asyncio.Queue(maxsize=max(1, config.concurrency) * 2)

    async def worker() -> None:
        nonlocal recognized, failed, last_error
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                await wait_while_paused(cancel, resume)
                if cancel.is_set():
                    continue
                try:
                    hits = await recognizer.recognize(item)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    async with lock:
                        failed += 1
                        recognized += 1
                        failed_indexes.append(item.index)
                        last_error = str(exc)
                        raw.append(manual_review_hit(item.start_sec, item.end_sec, str(exc)))
                    log.warning("frame %s retry failed: %s", item.index, exc)
                else:
                    async with lock:
                        recognized += 1
                        raw.extend(hits)
                finally:
                    try:
                        Path(item.path).unlink(missing_ok=True)
                    except OSError:
                        log.debug("failed to remove frame %s", item.path)
                async with lock:
                    done = recognized
                    bad = failed
                await publish("recognizing", f"正在重试失败的帧，已处理 {done - recognized_base}/{total}，失败 {bad} 帧")
            finally:
                queue.task_done()

    workers = [
        asyncio.create_task(worker(), name=f"retry-{slot}")
        for slot in range(max(1, config.concurrency))
    ]

    async def stop_workers_on_cancel() -> None:
        await cancel.wait()
        for task in workers:
            if not task.done():
                task.cancel()

    watcher = asyncio.create_task(stop_workers_on_cancel())
    try:
        try:
            for index in chosen:
                await wait_while_paused(cancel, resume)
                if cancel.is_set():
                    raise JobCancelled()
                dest = work_dir / f"retry_{index:06d}.jpg"
                try:
                    frame = await extract_one_frame(
                        video,
                        dest,
                        index=index,
                        fps=config.fps,
                        duration=probe.duration,
                        max_edge=recognition_max_edge(config.max_width, config.detail),
                        jpeg_q=config.jpeg_q,
                        cancel=cancel,
                        on_process=on_process,
                    )
                except FFmpegError as exc:
                    if cancel.is_set() or "已取消" in str(exc):
                        raise JobCancelled() from exc
                    bounds = frame_bounds(index, config.fps, probe.duration)
                    async with lock:
                        failed += 1
                        recognized += 1
                        failed_indexes.append(index)
                        last_error = str(exc)
                        if bounds is not None:
                            raw.append(manual_review_hit(bounds[0], bounds[1], str(exc)))
                    await publish("recognizing", f"正在重试失败的帧，失败 {failed} 帧")
                    continue
                while not cancel.is_set():
                    try:
                        await asyncio.wait_for(queue.put(frame), timeout=0.3)
                        break
                    except TimeoutError:
                        continue
                else:
                    raise JobCancelled()
            for _ in workers:
                while not cancel.is_set():
                    try:
                        await asyncio.wait_for(queue.put(None), timeout=0.3)
                        break
                    except TimeoutError:
                        continue
                else:
                    raise JobCancelled()
            await asyncio.gather(*workers)
        except asyncio.CancelledError:
            if cancel.is_set():
                raise JobCancelled() from None
            raise
    finally:
        watcher.cancel()
        for task in workers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await asyncio.gather(watcher, return_exceptions=True)

    if cancel.is_set():
        raise JobCancelled()
    findings = aggregate(raw)
    await publish("aggregating", f"重试结束，新命中 {len(findings)} 条")
    return findings, list(failed_indexes), last_error
