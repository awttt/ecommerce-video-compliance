"""FFmpeg 抽帧与探测。参数使用列表传递，中文路径不经过 shell。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from app.core.timeutil import format_fps
from app.schemas import Frame

_SCALE_DIVISIBLE: bool | None = None
_ENCODERS: set[str] | None = None


class FFmpegError(RuntimeError):
    pass


def decode_bytes(data: bytes) -> str:
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def resolve_bin(name: str) -> str:
    env_name = {"ffmpeg": "EVC_FFMPEG", "ffprobe": "EVC_FFPROBE"}.get(name, "")
    override = os.environ.get(env_name, "").strip() if env_name else ""
    if override:
        return override
    if getattr(sys, "frozen", False):
        suffix = ".exe" if os.name == "nt" else ""
        beside = Path(sys.executable).resolve().parent / f"{name}{suffix}"
        if beside.is_file():
            return str(beside)
    found = shutil.which(name)
    if not found:
        hint = f"，或设置环境变量 {env_name}" if env_name else ""
        raise FFmpegError(f"未找到 {name}。请安装 FFmpeg 并加入 PATH{hint}。")
    return found


def ffmpeg_available() -> bool:
    try:
        resolve_bin("ffmpeg")
        resolve_bin("ffprobe")
    except FFmpegError:
        return False
    return True


async def _run(cmd: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    text = decode_bytes(stdout) + decode_bytes(stderr)
    return proc.returncode or 0, text


async def scale_filter(max_edge: int) -> str:
    global _SCALE_DIVISIBLE
    if _SCALE_DIVISIBLE is None:
        try:
            code, text = await _run([resolve_bin("ffmpeg"), "-hide_banner", "-h", "filter=scale"])
            _SCALE_DIVISIBLE = code == 0 and "force_divisible_by" in text
        except FFmpegError:
            _SCALE_DIVISIBLE = False
    edge = max(2, int(max_edge))
    if _SCALE_DIVISIBLE:
        return (
            f"scale={edge}:{edge}:force_original_aspect_ratio=decrease:force_divisible_by=2"
        )
    return f"scale={edge}:{edge}:force_original_aspect_ratio=decrease"


class VideoProbe:
    def __init__(self, duration: float, has_video: bool, has_audio: bool, width: int, height: int):
        self.duration = duration
        self.has_video = has_video
        self.has_audio = has_audio
        self.width = width
        self.height = height


async def probe_video(path: Path) -> VideoProbe:
    cmd = [
        resolve_bin("ffprobe"),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    code, text = await _run(cmd)
    if code != 0:
        raise FFmpegError(text.strip() or "ffprobe 无法读取该视频")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FFmpegError("ffprobe 输出无法解析") from exc
    streams = payload.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = any(item.get("codec_type") == "audio" for item in streams)
    duration = 0.0
    raw_duration = (payload.get("format") or {}).get("duration")
    if raw_duration:
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError):
            duration = 0.0
    if video and duration <= 0:
        try:
            duration = float(video.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
    width = int(video.get("width") or 0) if video else 0
    height = int(video.get("height") or 0) if video else 0
    return VideoProbe(duration, video is not None, audio, width, height)


def _frame_indexes(directory: Path) -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for path in directory.glob("frame_*.jpg"):
        try:
            index = int(path.stem.split("_")[-1])
        except ValueError:
            continue
        found.append((index, path))
    found.sort()
    return found


def ready_frames(
    directory: Path,
    *,
    finished: bool,
    yielded: set[int],
    fps: float,
    duration: float,
) -> list[Frame]:
    items = _frame_indexes(directory)
    if not items:
        return []
    limit = len(items) if finished else len(items) - 1
    frames: list[Frame] = []
    for index, path in items[:limit]:
        if index in yielded or path.stat().st_size <= 0:
            continue
        start = (index - 1) / fps
        if duration > 0 and start >= duration - 1e-3:
            continue
        end = index / fps
        if duration > 0:
            end = min(end, duration)
        if end <= start:
            end = start + (1 / fps)
        frames.append(Frame(index=index, start_sec=round(start, 3), end_sec=round(end, 3), path=str(path)))
    return frames


async def extract_frames(
    video: Path,
    directory: Path,
    *,
    fps: float,
    max_edge: int,
    jpeg_q: int,
    duration: float,
    cancel: asyncio.Event,
    on_process: Callable[[asyncio.subprocess.Process | None], None] | None = None,
) -> AsyncIterator[Frame]:
    """边写 jpg 边交出已经写完的帧。最新一张在 ffmpeg 退出前视为未写完。"""
    directory.mkdir(parents=True, exist_ok=True)
    vf = f"fps={format_fps(fps)},{await scale_filter(max_edge)}"
    pattern = str(directory / "frame_%06d.jpg")
    cmd = [
        resolve_bin("ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(video),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        vf,
        "-q:v",
        str(int(jpeg_q)),
        "-start_number",
        "1",
        pattern,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    if on_process:
        on_process(proc)
    stderr_chunks: list[bytes] = []

    async def read_stderr() -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            stderr_chunks.append(line)
            if sum(len(chunk) for chunk in stderr_chunks) > 16_384:
                stderr_chunks.pop(0)

    reader = asyncio.create_task(read_stderr())
    yielded: set[int] = set()
    try:
        while True:
            if cancel.is_set():
                break
            finished = proc.returncode is not None
            for frame in ready_frames(
                directory,
                finished=finished,
                yielded=yielded,
                fps=fps,
                duration=duration,
            ):
                yielded.add(frame.index)
                yield frame
            if finished:
                break
            try:
                await asyncio.wait_for(proc.wait(), timeout=0.2)
            except TimeoutError:
                continue
        if cancel.is_set() and proc.returncode is None:
            proc.kill()
            await proc.wait()
        elif proc.returncode not in (0, None):
            detail = decode_bytes(b"".join(stderr_chunks)).strip()
            raise FFmpegError(detail or f"ffmpeg 抽帧失败，退出码 {proc.returncode}")
    finally:
        if proc.returncode is None:
            proc.kill()
            try:
                await proc.wait()
            except ProcessLookupError:
                pass
        if on_process:
            on_process(None)
        await reader


async def ffmpeg_encoders() -> set[str]:
    global _ENCODERS
    if _ENCODERS is None:
        names: set[str] = set()
        try:
            code, text = await _run([resolve_bin("ffmpeg"), "-hide_banner", "-encoders"])
        except FFmpegError:
            code, text = 1, ""
        if code == 0:
            for line in text.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0][:1] in {"A", "V", "S", "."}:
                    names.add(parts[1])
        _ENCODERS = names
    return _ENCODERS


async def speech_codec() -> tuple[str, list[str]]:
    """16kHz 单声道。优先 mp3，其次 aac，最后 wav。"""
    encoders = await ffmpeg_encoders()
    if "libmp3lame" in encoders:
        return ".mp3", ["-c:a", "libmp3lame", "-b:a", "32k"]
    if "aac" in encoders:
        return ".m4a", ["-c:a", "aac", "-b:a", "32k"]
    return ".wav", ["-c:a", "pcm_s16le"]


async def extract_audio(
    video: Path,
    dest: Path,
    *,
    codec_args: list[str],
    cancel: asyncio.Event,
    on_process: Callable[[asyncio.subprocess.Process | None], None] | None = None,
    start: float | None = None,
    duration: float | None = None,
) -> None:
    """抽出 16kHz 单声道语音。取消时结束 ffmpeg，不把非零退出当成失败。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        resolve_bin("ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(video),
    ]
    if start is not None and start > 0:
        cmd.extend(["-ss", f"{start:.3f}"])
    if duration is not None and duration > 0:
        cmd.extend(["-t", f"{duration:.3f}"])
    cmd.extend(["-vn", "-ac", "1", "-ar", "16000", *codec_args, str(dest)])
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    if on_process:
        on_process(proc)
    stderr_chunks: list[bytes] = []

    async def read_stderr() -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            stderr_chunks.append(line)
            if sum(len(chunk) for chunk in stderr_chunks) > 16_384:
                stderr_chunks.pop(0)

    reader = asyncio.create_task(read_stderr())
    try:
        while proc.returncode is None:
            if cancel.is_set():
                break
            try:
                await asyncio.wait_for(proc.wait(), timeout=0.3)
            except TimeoutError:
                continue
        if cancel.is_set() and proc.returncode is None:
            proc.kill()
            await proc.wait()
            return
        if proc.returncode not in (0, None):
            detail = decode_bytes(b"".join(stderr_chunks)).strip()
            raise FFmpegError(detail or f"ffmpeg 提取音频失败，退出码 {proc.returncode}")
        if not dest.is_file() or dest.stat().st_size <= 0:
            raise FFmpegError("没有提取到音频")
    finally:
        if proc.returncode is None:
            proc.kill()
            try:
                await proc.wait()
            except ProcessLookupError:
                pass
        if on_process:
            on_process(None)
        await reader
