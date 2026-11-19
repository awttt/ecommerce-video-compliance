"""找出只闪一两帧的后期贴字。

镜头可以不动，整幅画面也可以差不多。比较的是大约 32 像素的小块：
某一块突然变亮或变暗，短则一帧、长则三帧，随后又回到贴上去之前的亮度。
普通抽帧仍按 0.5–2 FPS 走。这里只补那些落在两次取样之间的帧。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# 扫描图长边。再小的话，价格几个字缩没了，小块均值几乎不动。
SCAN_EDGE = 640
BLOCK = 32
STRIDE = 2
# 小块平均亮度至少变这么多，才算贴上了东西。压缩噪声通常远小于这个值。
CHANGE = 24
# 贴字最多连续这么多帧。再长就交给普通抽帧。
MAX_RUN = 3
# 一次视频最多把变化最明显的这么多处送去识别。
MAX_FLASHES = 40
# 闪帧在失败重试里用这个基数以上的帧号，避免和 0.5–2 FPS 的帧号混在一起。
FLASH_INDEX_BASE = 1_000_000


@dataclass(frozen=True)
class FlashRun:
    start_index: int
    length: int
    score: int


@dataclass(frozen=True)
class FlashSpan:
    start_sec: float
    end_sec: float
    source_index: int
    score: int
    # 运动扫描才有：相对整帧的裁切（左、上、宽、高）。亮度那一路保持整帧。
    crop: tuple[float, float, float, float] | None = None


def scan_size(width: int, height: int, max_edge: int = SCAN_EDGE) -> tuple[int, int]:
    """扫描用的偶数宽高。长边不超过 max_edge，比原片小就用原片。"""
    if width < 2 or height < 2:
        return 0, 0
    scale = min(1.0, max_edge / max(width, height))
    scanned_w = max(2, int(width * scale) // 2 * 2)
    scanned_h = max(2, int(height * scale) // 2 * 2)
    return scanned_w, scanned_h


def block_means(frame: bytes, width: int, height: int, block: int = BLOCK, stride: int = STRIDE) -> list[int]:
    """每个小块最暗、最亮各约八分之一像素的平均亮度。

    整块平均会被没变的背景拉平，几个字就看不见。浅色字抬高最亮的一段，
    深色字压低最暗的一段。边缘不足一块的也算，避免贴在角落。
    """
    if width < 2 or height < 2 or len(frame) < width * height:
        return []
    view = memoryview(frame)
    cols = (width + block - 1) // block
    rows = (height + block - 1) // block
    means: list[int] = []
    for by in range(rows):
        y0 = by * block
        y1 = min(y0 + block, height)
        for bx in range(cols):
            x0 = bx * block
            x1 = min(x0 + block, width)
            samples: list[int] = []
            for y in range(y0, y1, stride):
                samples.extend(view[y * width + x0 : y * width + x1 : stride])
            if not samples:
                means.extend((0, 0))
                continue
            samples.sort()
            take = max(1, len(samples) // 8)
            dark = sum(samples[:take]) // take
            bright = sum(samples[-take:]) // take
            means.append(dark)
            means.append(bright)
    return means


def _block_flashed(before: int, middle: int, after: int) -> bool:
    """这一小块在中间帧变了，前后两帧彼此仍接近。"""
    jumped = abs(middle - before)
    returned = abs(middle - after)
    stayed = abs(after - before)
    if jumped < CHANGE or returned < CHANGE:
        return False
    return stayed * 2 <= min(jumped, returned)


def _run_score(before: list[int], middles: list[list[int]], after: list[int]) -> int | None:
    if not before or not middles or len(after) != len(before):
        return None
    if any(len(middle) != len(before) for middle in middles):
        return None
    peaks: list[int] = []
    for block in range(len(before)):
        if not all(_block_flashed(before[block], middle[block], after[block]) for middle in middles):
            continue
        peaks.append(max(abs(middle[block] - before[block]) for middle in middles))
    if not peaks:
        return None
    return max(peaks)


class FlashScanner:
    """逐帧推进。只留尚未判定的几帧，不把整段视频的小块都留在内存里。"""

    def __init__(self) -> None:
        self._series: list[list[int]] = []
        self._origin = 0
        self._cursor = 0
        self._found: list[FlashRun] = []

    def push(self, means: list[int]) -> None:
        if not means:
            return
        if self._series and len(means) != len(self._series[0]):
            return
        self._series.append(means)
        self._drain(final=False)

    def finish(self) -> list[FlashRun]:
        self._drain(final=True)
        found = sorted(self._found, key=lambda item: item.score, reverse=True)
        return found[:MAX_FLASHES]

    def _drain(self, *, final: bool) -> None:
        while True:
            rel = self._cursor - self._origin
            if rel < 0:
                rel = 0
                self._cursor = self._origin
            available = len(self._series) - rel
            if available < 3:
                break
            limit = min(MAX_RUN, available - 2)
            if not final and available < MAX_RUN + 2:
                limit = min(limit, available - 2)
            hit: FlashRun | None = None
            for length in range(1, limit + 1):
                if not final and length > 1 and available < length + 2:
                    break
                after_rel = rel + length + 1
                if after_rel >= len(self._series):
                    break
                score = _run_score(
                    self._series[rel],
                    self._series[rel + 1 : rel + 1 + length],
                    self._series[after_rel],
                )
                if score is None:
                    continue
                hit = FlashRun(self._cursor + 1, length, score)
                break
            if hit is None:
                # 短窗口里一帧、两帧都不像时，先等后面的帧，避免把三帧的贴字拆掉。
                if not final and available < MAX_RUN + 2:
                    break
                self._cursor += 1
            else:
                self._found.append(hit)
                # 只前进到命中的第一帧。下一帧仍可能是另一处贴字，要再当一次中间帧。
                # 连续两三帧的同一处贴字不会重复：下一轮的前帧自己就带着字，跳变不够。
                self._cursor = hit.start_index
            drop = self._cursor - self._origin
            if drop > 0:
                del self._series[:drop]
                self._origin = self._cursor


def merge_flash_spans(
    primary: list[FlashSpan],
    secondary: list[FlashSpan],
    *,
    limit: int = MAX_FLASHES,
) -> list[FlashSpan]:
    """两路分数不能混在一起排。先留 primary，再用 secondary 补到 limit，按源帧号去重。

    secondary 按它自己的分数从高往低补。按时间补的话，名额不够时丢的总是片尾。
    """
    chosen: list[FlashSpan] = []
    seen: set[int] = set()
    fill = sorted(secondary, key=lambda item: item.score, reverse=True)
    for span in list(primary) + fill:
        if span.source_index in seen:
            continue
        chosen.append(span)
        seen.add(span.source_index)
        if len(chosen) >= limit:
            break
    return sorted(chosen, key=lambda item: (item.start_sec, item.source_index))


def flash_frame_index(start_sec: float) -> int:
    """用起始毫秒做重试帧号。0.5–2 FPS 的帧号到不了这个范围。"""
    millis = int(round(max(0.0, float(start_sec)) * 1000))
    return FLASH_INDEX_BASE + millis


def flash_time(index: int) -> float | None:
    """普通抽帧帧号返回 None。闪帧帧号还原成起始秒。"""
    if index < FLASH_INDEX_BASE:
        return None
    return (index - FLASH_INDEX_BASE) / 1000


def _at(index: int, times: list[float], fps: float) -> float:
    if 0 <= index < len(times):
        return times[index]
    if fps > 0:
        return index / fps
    return 0.0


def covered_by_samples(start: float, end: float, sample_fps: float) -> bool:
    """普通抽帧的取样点落在这段里时，这帧已经会送给模型。"""
    if sample_fps <= 0 or end <= start:
        return False
    first = math.ceil((start - 1e-3) * sample_fps)
    if first < 0:
        first = 0
    return first / sample_fps < end - 1e-4


def spans_from_runs(
    runs: list[FlashRun],
    times: list[float],
    *,
    fps: float,
    duration: float,
    sample_fps: float,
) -> list[FlashSpan]:
    """把命中的源帧收成真实起止。已经落在普通抽帧网格上的不再重复送。"""
    ranked = sorted(runs, key=lambda item: item.score, reverse=True)
    chosen: dict[int, FlashSpan] = {}
    for run in ranked:
        if run.length < 1 or run.start_index < 0:
            continue
        start = _at(run.start_index, times, fps)
        end = _at(run.start_index + run.length, times, fps)
        if end <= start:
            step = run.length / fps if fps > 0 else 0.04
            end = start + step
        if duration > 0:
            if start >= duration:
                continue
            end = min(end, duration)
        if end <= start:
            end = start + 0.001
        if covered_by_samples(start, end, sample_fps):
            continue
        span = FlashSpan(
            start_sec=round(start, 3),
            end_sec=round(end, 3),
            source_index=run.start_index + (run.length - 1) // 2,
            score=run.score,
        )
        key = flash_frame_index(span.start_sec)
        current = chosen.get(key)
        if current is None or span.score > current.score:
            chosen[key] = span
    return sorted(chosen.values(), key=lambda item: (item.start_sec, item.source_index))
