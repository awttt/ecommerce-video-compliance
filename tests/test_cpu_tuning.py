"""CPU 优化功能的测试。"""

import os
import sys
import subprocess
import pytest

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
        """设置无效的 EVC_FFMPEG_THREADS 环境变量，回到默认公式。"""
        monkeypatch.setenv("EVC_FFMPEG_THREADS", "invalid")
        result = ffmpeg_threads()
        # 应该使用默认公式：max(1, min(4, (cpu_count or 2) // 2))
        assert result >= 1
        assert result <= 4

    def test_ffmpeg_threads_env_zero(self, monkeypatch):
        """设置 0，应该回到默认公式。"""
        monkeypatch.setenv("EVC_FFMPEG_THREADS", "0")
        result = ffmpeg_threads()
        assert result >= 1
        assert result <= 4

    def test_ffmpeg_threads_no_env(self, monkeypatch):
        """不设置环境变量，使用默认公式。"""
        monkeypatch.delenv("EVC_FFMPEG_THREADS", raising=False)
        result = ffmpeg_threads()
        assert result >= 1
        assert result <= 4

    def test_ffmpeg_threads_env_large(self, monkeypatch):
        """设置很大的值，但应该被 min(4, ...) 限制。"""
        monkeypatch.setenv("EVC_FFMPEG_THREADS", "100")
        result = ffmpeg_threads()
        assert result == 100  # 环境变量优先，不再限制为 4


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
