"""按命中时间段切除画面并重新拼接。

这是「一键生成无违规视频」的剪辑入口。检测结果只提供时间区间，
真正的编码放在这里，避免和识别流水线缠在一起。
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from app.core.ffmpeg import FFmpegError, VideoProbe, decode_bytes, resolve_bin
from app.schemas import Finding

log = logging.getLogger(__name__)

# 口播识别的句尾经常停在最后一个字还没说完的地方。向后多切大约一个字，
# 但有下一句、并且中间有停顿时，切在停顿上，不切进下一句的起音。
_SPEECH_TAIL = 0.26
_SPEECH_LEAD = 0.05
_ONSET_GUARD = 0.04
_GAP_TRUST = 0.12
_SILENCE_LOOKAHEAD = 0.55
_SHORT_SILENCE = 0.5
_SILENCE_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


class EditorError(RuntimeError):
    pass


def merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: list[tuple[float, float]] = [ordered[0]]
    for start, end in ordered[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + 1e-3:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def plan_cuts(
    duration: float,
    findings: list[Finding],
    padding: float,
    min_keep: float = 0.12,
    silences: list[tuple[float, float]] | None = None,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """返回 (要删掉的区间, 要保留的区间)。

    画面命中向两侧加同样的边距。口播命中按整句切：句尾再吃掉大约一个字的尾音，
    下一句已经开始时则停在两句之间的停顿，避免把 A 的最后一个字留在 B 的开头。
    """
    if duration <= 0:
        raise EditorError("视频时长无效，无法剪辑")
    if not findings:
        raise EditorError("没有命中区间，无需剪辑")
    pauses = sorted(silences or [])
    cuts: list[tuple[float, float]] = []
    for item in findings:
        window = _speech_window(item, padding, duration, pauses) if item.source == "口播" else _picture_window(item, padding, duration)
        if window is not None:
            cuts.append(window)
    cuts = merge_intervals(cuts)
    keeps: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in cuts:
        if start - cursor >= min_keep:
            keeps.append((round(cursor, 3), round(start, 3)))
        cursor = max(cursor, end)
    if duration - cursor >= min_keep:
        keeps.append((round(cursor, 3), round(duration, 3)))
    if not keeps:
        raise EditorError("命中区间覆盖了整个视频，没有可保留的画面")
    return cuts, keeps


def _picture_window(item: Finding, padding: float, duration: float) -> tuple[float, float] | None:
    start = max(0.0, item.start_sec - max(padding, 0.0))
    end = min(duration, item.end_sec + max(padding, 0.0))
    if end - start <= 0.01:
        return None
    return start, end


def _speech_window(
    item: Finding,
    padding: float,
    duration: float,
    silences: list[tuple[float, float]],
) -> tuple[float, float] | None:
    start = item.start_sec
    end = item.end_sec
    if item.sentence_start is not None:
        start = min(start, item.sentence_start)
    if item.sentence_end is not None:
        end = max(end, item.sentence_end)
    lead = min(max(padding, 0.0), _SPEECH_LEAD)
    cut_start = max(0.0, start - lead)
    cut_end = _speech_end(end, item.next_start, duration, silences, padding)
    if cut_end - cut_start <= 0.01:
        return None
    return cut_start, cut_end


def _speech_end(
    end: float,
    next_start: float | None,
    duration: float,
    silences: list[tuple[float, float]],
    padding: float,
) -> float:
    tail = max(_SPEECH_TAIL, max(padding, 0.0))
    pause = _pause_after(end, next_start, silences)
    if pause is not None:
        silence_start, silence_end = pause
        cut = silence_end if silence_end - silence_start <= _SHORT_SILENCE else silence_start + 0.04
        if next_start is not None and next_start + 1e-3 >= silence_start:
            cut = min(cut, max(end, next_start - _ONSET_GUARD))
        return min(duration, max(end, cut))
    cut = end + tail
    if next_start is not None and next_start > end + _GAP_TRUST:
        cut = min(cut, next_start - _ONSET_GUARD)
    return min(duration, max(end, cut))


def _pause_after(
    end: float,
    next_start: float | None,
    silences: list[tuple[float, float]],
) -> tuple[float, float] | None:
    horizon = end + _SILENCE_LOOKAHEAD
    for start, stop in silences:
        if stop < end - 0.02:
            continue
        if start > horizon:
            break
        if start < end - 0.08:
            continue
        if next_start is not None and next_start > end + _GAP_TRUST and start > next_start - 0.02:
            continue
        if stop >= end - 0.02:
            return start, stop
    return None


def _parse_silences(text: str) -> list[tuple[float, float]]:
    starts = [float(item) for item in _SILENCE_START.findall(text)]
    ends = [float(item) for item in _SILENCE_END.findall(text)]
    pairs: list[tuple[float, float]] = []
    for index, start in enumerate(starts):
        if index >= len(ends):
            break
        stop = ends[index]
        if stop > start:
            pairs.append((start, stop))
    return pairs


async def detect_silences(source: Path) -> list[tuple[float, float]]:
    """找出句间停顿。失败时返回空列表，剪辑改用句尾补偿。"""
    try:
        binary = resolve_bin("ffmpeg")
    except FFmpegError:
        return []
    cmd = [
        binary,
        "-hide_banner",
        "-nostats",
        "-i",
        str(source),
        "-af",
        "silencedetect=noise=-32dB:d=0.05",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
    except OSError:
        log.warning("silence detect failed to start")
        return []
    return _parse_silences(decode_bytes(stderr))


def _filter_graph(keeps: list[tuple[float, float]], has_audio: bool) -> str:
    parts: list[str] = []
    video_labels: list[str] = []
    audio_labels: list[str] = []
    for index, (start, end) in enumerate(keeps):
        parts.append(f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{index}]")
        video_labels.append(f"[v{index}]")
        if has_audio:
            parts.append(
                f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{index}]"
            )
            audio_labels.append(f"[a{index}]")
    if has_audio:
        paired = "".join(video + audio for video, audio in zip(video_labels, audio_labels))
        parts.append(f"{paired}concat=n={len(keeps)}:v=1:a=1[outv][outa]")
    else:
        parts.append(f"{''.join(video_labels)}concat=n={len(keeps)}:v=1:a=0[outv]")
    return ";".join(parts)


async def render_clean_video(
    source: Path,
    output: Path,
    keeps: list[tuple[float, float]],
    probe: VideoProbe,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        await _render(source, output, keeps, has_audio=probe.has_audio)
    except FFmpegError:
        if not probe.has_audio:
            raise
        await _render(source, output, keeps, has_audio=False)


async def _render(
    source: Path,
    output: Path,
    keeps: list[tuple[float, float]],
    *,
    has_audio: bool,
) -> None:
    graph = _filter_graph(keeps, has_audio)
    cmd = [
        resolve_bin("ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-filter_complex",
        graph,
        "-map",
        "[outv]",
    ]
    if has_audio:
        cmd.extend(["-map", "[outa]", "-c:a", "aac"])
    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        detail = decode_bytes(stderr).strip()
        raise FFmpegError(detail or f"ffmpeg 剪辑失败，退出码 {proc.returncode}")
