"""本机线程上限。不导入 numpy，方便在它加载前生效。"""

from __future__ import annotations

import os

NATIVE_THREAD_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)


def thread_override() -> int | None:
    """EVC_FFMPEG_THREADS。1 到 8 原样返回，大于 8 记为 8。

    缺省、0、负数和无法解析时返回 None，由调用方用默认公式。
    """
    raw = os.environ.get("EVC_FFMPEG_THREADS", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    if value < 1:
        return None
    return min(8, value)


def default_thread_count() -> int:
    """和 ffmpeg 默认公式相同：不超过 4，也不超过一半逻辑核。"""
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def slot_count() -> int:
    """同时运行的 ffmpeg 个数。EVC_FFMPEG_SLOTS 夹在 1 到 4，缺省或无法解析时为 2。"""
    raw = os.environ.get("EVC_FFMPEG_SLOTS", "").strip()
    if not raw:
        return 2
    try:
        value = int(raw)
    except ValueError:
        return 2
    return min(4, max(1, value))


def limit_native_threads() -> None:
    """给尚未加载的 OpenBLAS / OpenMP 一个上限。已经设过的变量保持原值。"""
    override = thread_override()
    count = str(default_thread_count() if override is None else override)
    for name in NATIVE_THREAD_VARS:
        os.environ.setdefault(name, count)
