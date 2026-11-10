"""时间码。展示用 MM:SS，超过 1 小时用 HH:MM:SS。"""

from __future__ import annotations

import math
from datetime import datetime, timezone


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_clock(total_seconds: int) -> str:
    total_seconds = max(0, int(total_seconds))
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def format_range(start: float, end: float) -> str:
    """闭开区间覆盖到整秒。12.0–15.0 显示为 00:12-00:15。"""
    start_sec = int(math.floor(max(0.0, start) + 1e-6))
    end_sec = int(math.ceil(max(start, end) - 1e-6))
    if end_sec < start_sec:
        end_sec = start_sec
    return f"{format_clock(start_sec)}-{format_clock(end_sec)}"


def format_fps(fps: float) -> str:
    return f"{fps:.2f}".rstrip("0").rstrip(".")
