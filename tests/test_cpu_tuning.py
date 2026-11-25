"""CPU 优化功能的测试。"""

import asyncio
import os
import sys
import subprocess
import pytest

from app.core.cpu_limits import NATIVE_THREAD_VARS, limit_native_threads, slot_count
from app.core.ffmpeg import ffmpeg_threads, hidden_process_kwargs
from app.core.flash import block_means, _block_means_py


class TestBlockMeans:
    """测试 block_means numpy 版与纯 Python 版的等价性。"""

    def _random_frame(self, width: int, height: int) -> bytes:
        """生成随机灰度图帧。"""
        import random
        return bytes(random.randint(0, 255) for _ in range(width * height))

    @pytest.mark.parametrize("width,height", [
        (640, 360),
        (360, 640),
        (33, 47),
        (2, 2),
        (31, 65),
    ])
    def test_block_means_equivalence(self, width: int, height: int):
        """验证 block_means numpy 版与纯 Python 版结果完全相等。"""
        frame = self._random_frame(width, height)

        # 获取两个版本的结果
        result_py = _block_means_py(frame, width, height)
        result_np = block_means(frame, width, height)

        # 结果应该完全相等
        assert result_py == result_np, f"结果不匹配，尺寸 {width}x{height}"

        # 验证返回值都是 int 类型（Python int，不是 numpy 类型）
        for val in result_np:
            assert isinstance(val, int), f"返回值不是 Python int: {type(val)}"

    def test_block_means_empty_frame(self):
        """测试空帧处理。"""
        frame = b""
        result = block_means(frame, 10, 10)
        assert result == []

    def test_block_means_small_frame(self):
        """测试 2x2 最小帧。"""
        # 2x2 灰度图：4 个像素
        frame = bytes([100, 150, 120, 180])
        result_py = _block_means_py(frame, 2, 2)
        result_np = block_means(frame, 2, 2)
        assert result_py == result_np
        assert len(result_np) == 2  # 一个 2x2 块，返回 dark 和 bright


class TestFfmpegThreads:
    """测试 ffmpeg 线程数限制。"""

    def test_ffmpeg_threads_env_valid(self, monkeypatch):
        """设置有效的 EVC_FFMPEG_THREADS 环境变量。"""
        monkeypatch.setenv("EVC_FFMPEG_THREADS", "3")
        assert ffmpeg_threads() == 3

    def test_ffmpeg_threads_env_invalid(self, monkeypatch):
        """设置无效的 EVC_FFMPEG_THREADS 环境变量，回到默认 2。"""
        monkeypatch.setenv("EVC_FFMPEG_THREADS", "invalid")
        assert ffmpeg_threads() == 2

    def test_ffmpeg_threads_env_zero(self, monkeypatch):
        """设置 0，应该回到默认 2。"""
        monkeypatch.setenv("EVC_FFMPEG_THREADS", "0")
        assert ffmpeg_threads() == 2

    def test_ffmpeg_threads_no_env(self, monkeypatch):
        """不设置环境变量时固定为 2，不随 CPU 核数变化。"""
        monkeypatch.delenv("EVC_FFMPEG_THREADS", raising=False)
        monkeypatch.setattr(os, "cpu_count", lambda: 12)
        assert ffmpeg_threads() == 2

    def test_ffmpeg_threads_env_large(self, monkeypatch):
        """超过 8 的环境变量按 8 封顶，不再原样放开。"""
        monkeypatch.setenv("EVC_FFMPEG_THREADS", "100")
        assert ffmpeg_threads() == 8

    def test_ffmpeg_threads_env_negative_uses_default(self, monkeypatch):
        """负数无法作为线程数，回到默认 2。"""
        monkeypatch.setenv("EVC_FFMPEG_THREADS", "-3")
        assert ffmpeg_threads() == 2


class TestHiddenProcessKwargs:
    """测试 Windows 进程参数设置。"""

    def test_hidden_process_kwargs_non_windows(self, monkeypatch):
        """非 Windows 系统：没有 EVC_FFMPEG_LOW_PRIORITY 限制时返回空字典。"""
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.delenv("EVC_FFMPEG_LOW_PRIORITY", raising=False)
        result = hidden_process_kwargs()
        assert result == {}

    def test_hidden_process_kwargs_windows_with_priority(self, monkeypatch):
        """Windows 系统且 EVC_FFMPEG_LOW_PRIORITY!=0 时，应包含 BELOW_NORMAL 位。"""
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("EVC_FFMPEG_LOW_PRIORITY", "1")
        result = hidden_process_kwargs()

        assert "creationflags" in result
        # 应该包含 CREATE_NO_WINDOW 或 BELOW_NORMAL_PRIORITY_CLASS 的位
        # BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        below_normal = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
        assert (result["creationflags"] & below_normal) == below_normal

    def test_hidden_process_kwargs_windows_no_priority(self, monkeypatch):
        """Windows 系统且 EVC_FFMPEG_LOW_PRIORITY=0 时，不应包含 BELOW_NORMAL 位。"""
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("EVC_FFMPEG_LOW_PRIORITY", "0")
        result = hidden_process_kwargs()

        # 应该只有 CREATE_NO_WINDOW，没有 BELOW_NORMAL_PRIORITY_CLASS
        below_normal = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
        assert (result["creationflags"] & below_normal) == 0


def test_slot_count_clamps_between_one_and_four(monkeypatch):
    monkeypatch.delenv("EVC_FFMPEG_SLOTS", raising=False)
    assert slot_count() == 2
    monkeypatch.setenv("EVC_FFMPEG_SLOTS", "3")
    assert slot_count() == 3
    monkeypatch.setenv("EVC_FFMPEG_SLOTS", "9")
    assert slot_count() == 4
    monkeypatch.setenv("EVC_FFMPEG_SLOTS", "0")
    assert slot_count() == 1
    monkeypatch.setenv("EVC_FFMPEG_SLOTS", "nope")
    assert slot_count() == 2


def test_native_thread_vars_fill_blanks_and_keep_explicit_values(monkeypatch):
    for name in NATIVE_THREAD_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EVC_FFMPEG_THREADS", "1")
    monkeypatch.setenv("OMP_NUM_THREADS", "3")
    limit_native_threads()
    assert os.environ["OMP_NUM_THREADS"] == "3"
    for name in NATIVE_THREAD_VARS:
        if name == "OMP_NUM_THREADS":
            continue
        assert os.environ[name] == "1"


async def test_ffmpeg_slot_lets_only_the_configured_number_overlap(monkeypatch):
    import app.core.ffmpeg as ffmpeg

    monkeypatch.setenv("EVC_FFMPEG_SLOTS", "1")
    current = 0
    peak = 0

    async def hold():
        nonlocal current, peak
        async with ffmpeg.ffmpeg_slot():
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(0.05)
            current -= 1

    await asyncio.gather(*(hold() for _ in range(3)))
    assert peak == 1


async def test_extract_strip_adds_thread_args_and_takes_a_slot(monkeypatch, tmp_path):
    import app.core.ffmpeg as ffmpeg

    monkeypatch.setattr(ffmpeg, "_VFR_ARGS", ["-fps_mode", "vfr"])
    monkeypatch.setenv("EVC_FFMPEG_THREADS", "2")
    seen: dict = {}

    async def fake_run(cmd, *, slot=False):
        seen["cmd"] = cmd
        seen["slot"] = slot
        (tmp_path / "strip_0000.jpg").write_bytes(b"x")
        return 0, ""

    monkeypatch.setattr(ffmpeg, "_run", fake_run)
    monkeypatch.setattr(ffmpeg, "resolve_bin", lambda _name: "ffmpeg")
    count = await ffmpeg.extract_strip(tmp_path / "in.mp4", tmp_path)
    assert count == 1
    assert seen["slot"] is True
    assert seen["cmd"][6:8] == ["-threads", "2"]
    assert seen["cmd"][-3:-1] == ["-threads", "2"]
