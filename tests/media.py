"""测试用的小视频。没有 ffmpeg 时跳过。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def ffmpeg_bin() -> str | None:
    return shutil.which("ffmpeg")


def has_encoder(name: str) -> bool:
    binary = ffmpeg_bin()
    if not binary:
        return False
    result = subprocess.run(
        [binary, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        check=False,
    )
    return name in result.stdout


def make_video(path: Path, seconds: float = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    binary = ffmpeg_bin()
    if binary is None:
        raise RuntimeError("没有 ffmpeg")
    subprocess.run(
        [
            binary,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s=320x240:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=mono",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
    )


def probe_duration(path: Path) -> float:
    binary = shutil.which("ffprobe")
    if binary is None:
        raise RuntimeError("没有 ffprobe")
    result = subprocess.run(
        [
            binary,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nokey=1:noprint_wrappers=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())
