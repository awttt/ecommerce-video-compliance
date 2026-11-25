"""FFmpeg 抽帧与探测。参数使用列表传递，中文路径不经过 shell。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from app.core.flash import (
    FlashSpan,
    FlashScanner,
    block_means,
    flash_frame_index,
    merge_flash_spans,
    scan_size,
    spans_from_runs,
)
from app.core.motion import (
    MotionScanner,
    available as motion_available,
    magnify_flash,
    motion_runs,
    stamp_crops,
)
from app.schemas import Frame

log = logging.getLogger(__name__)

_PTS_TIME = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")

_SCALE_DIVISIBLE: bool | None = None
_ENCODERS: set[str] | None = None
_VFR_ARGS: list[str] | None = None


class FFmpegError(RuntimeError):
    pass


def decode_bytes(data: bytes) -> str:
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# 从访达打开 .app 时 PATH 里没有 Homebrew。只在打包后的程序里查找这些目录。
_DARWIN_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def _frozen_bin_candidates(name: str) -> list[Path]:
    suffix = ".exe" if os.name == "nt" else ""
    filename = f"{name}{suffix}"
    exe_dir = Path(sys.executable).resolve().parent
    candidates = [exe_dir / filename]
    if sys.platform == "darwin" and exe_dir.name == "MacOS" and exe_dir.parent.name == "Contents":
        candidates.append(exe_dir.parent.parent.parent / filename)
        candidates.extend(Path(prefix) / filename for prefix in _DARWIN_BIN_DIRS)
    return candidates


def resolve_bin(name: str) -> str:
    env_name = {"ffmpeg": "EVC_FFMPEG", "ffprobe": "EVC_FFPROBE"}.get(name, "")
    override = os.environ.get(env_name, "").strip() if env_name else ""
    if override:
        return override
    if getattr(sys, "frozen", False):
        for candidate in _frozen_bin_candidates(name):
            if candidate.is_file():
                return str(candidate)
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


def ffmpeg_threads() -> int:
    """获取 ffmpeg 线程数限制。

    优先使用环境变量 EVC_FFMPEG_THREADS（必须是正整数），
    否则使用 max(1, min(4, (os.cpu_count() or 2) // 2))
    """
    env_val = os.environ.get("EVC_FFMPEG_THREADS", "").strip()
    if env_val:
        try:
            val = int(env_val)
            if val > 0:
                return val
        except ValueError:
            pass
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def thread_args() -> list[str]:
    """返回 ffmpeg 线程参数。"""
    return ["-threads", str(ffmpeg_threads())]


def hidden_process_kwargs() -> dict[str, int]:
    """Windows 上启动 ffmpeg 时不分配控制台，避免打包后的窗口程序弹出黑框。

    同时在 Windows 上降低优先级，除非 EVC_FFMPEG_LOW_PRIORITY=0。
    """
    if sys.platform != "win32":
        return {}
    kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    if os.environ.get("EVC_FFMPEG_LOW_PRIORITY", "1") != "0":
        # 按位或上 BELOW_NORMAL_PRIORITY_CLASS
        below_normal = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
        kwargs["creationflags"] |= below_normal
    return kwargs


async def spawn_process(
    *cmd: str,
    stdout: int | None = asyncio.subprocess.PIPE,
    stderr: int | None = asyncio.subprocess.PIPE,
) -> asyncio.subprocess.Process:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        **hidden_process_kwargs(),
    )
    # 非 Windows 系统上降低优先级，除非 EVC_FFMPEG_LOW_PRIORITY=0
    if sys.platform != "win32" and os.environ.get("EVC_FFMPEG_LOW_PRIORITY", "1") != "0":
        try:
            os.setpriority(os.PRIO_PROCESS, proc.pid, 10)
        except (OSError, AttributeError):
            log.debug("设置进程优先级失败")
    return proc


async def _run(cmd: list[str]) -> tuple[int, str]:
    proc = await spawn_process(*cmd)
    stdout, stderr = await proc.communicate()
    text = decode_bytes(stdout) + decode_bytes(stderr)
    return proc.returncode or 0, text


def sample_filter(fps: float) -> str:
    """从 0 秒起，每隔 1/fps 秒取当时那一帧。

    不用 ffmpeg 的 fps 滤镜。它的相位跟着输出间隔走，1 FPS 抽到的画面
    不在 2 FPS 那一组里，所以更高密度反而会漏掉低密度已经看见的字。
    网格对齐之后，2 FPS 的整数秒和 1 FPS 是同一张图。
    """
    interval = max((1.0 / float(fps)) - 0.001, 0.001)
    return f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{interval:.6f})'"


async def vfr_args() -> list[str]:
    """选出的帧按原时间戳写出。不补成固定帧率，否则同一张图会被重复写很多遍。"""
    global _VFR_ARGS
    if _VFR_ARGS is None:
        try:
            code, text = await _run([resolve_bin("ffmpeg"), "-hide_banner", "-h", "full"])
        except FFmpegError:
            code, text = 1, ""
        if code == 0 and "-fps_mode" in text:
            _VFR_ARGS = ["-fps_mode", "vfr"]
        else:
            _VFR_ARGS = ["-vsync", "vfr"]
    return list(_VFR_ARGS)


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
    def __init__(
        self,
        duration: float,
        has_video: bool,
        has_audio: bool,
        width: int,
        height: int,
        fps: float = 0,
        codec: str = "",
        pix_fmt: str = "",
        profile: str = "",
    ):
        self.duration = duration
        self.has_video = has_video
        self.has_audio = has_audio
        self.width = width
        self.height = height
        self.fps = fps
        self.codec = codec
        self.pix_fmt = pix_fmt
        self.profile = profile


def _parse_rate(value: object) -> float:
    text = str(value or "").strip()
    if not text or text == "0/0":
        return 0.0
    if "/" in text:
        num, den = text.split("/", 1)
        try:
            num_f = float(num)
            den_f = float(den)
        except ValueError:
            return 0.0
        if den_f == 0:
            return 0.0
        return num_f / den_f
    try:
        return float(text)
    except ValueError:
        return 0.0


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
    fps = 0.0
    if video:
        fps = _parse_rate(video.get("avg_frame_rate"))
        if fps <= 0:
            fps = _parse_rate(video.get("r_frame_rate"))
        if fps <= 0 and duration > 0:
            try:
                count = int(video.get("nb_frames") or 0)
            except (TypeError, ValueError):
                count = 0
            if count > 0:
                fps = count / duration
    codec = str(video.get("codec_name") or "") if video else ""
    pix_fmt = str(video.get("pix_fmt") or "") if video else ""
    profile = str(video.get("profile") or "") if video else ""
    return VideoProbe(duration, video is not None, audio, width, height, fps, codec, pix_fmt, profile)


# 窗口只稳定画出这几种。其余的 MP4 往往有声音、画面是黑的。
_PREVIEW_EDGE = 1280


def window_can_play(path: Path, probe: VideoProbe) -> bool:
    """WebView2 能直接画出来的画面。MP4 外壳不够，还要看编码和像素格式。

    WebView2（Chromium）对 H.264 的支持:
    - Baseline/Constrained Baseline Profile: 最佳兼容性
    - Main/High Profile: 可能出现黑屏（特别是没有硬件加速时）

    为确保所有用户都能正常播放，只允许 Baseline Profile 直接播放，
    其他 Profile 统一转码。
    """
    suffix = path.suffix.lower()
    codec = probe.codec.lower()
    pix = probe.pix_fmt.lower()
    profile = probe.profile.lower()

    # 只允许 MP4/M4V + H.264 Baseline Profile + yuv420p 直接播放
    if suffix in {".mp4", ".m4v"} and codec == "h264" and pix in {"yuv420p", "yuvj420p"}:
        # 检查 profile：只有 Baseline 或 Constrained Baseline 才能直接播放
        if "baseline" in profile or "constrained" in profile:
            return True
        # Main, High, High 10 等其他 Profile 需要转码
        return False

    # WebM (VP8/VP9) 通常没问题
    return suffix == ".webm" and codec in {"vp8", "vp9"}


async def transcode_preview(
    source: Path,
    dest: Path,
    *,
    has_audio: bool,
    cancel: asyncio.Event,
    on_process: Callable[[asyncio.subprocess.Process], None] | None = None,
) -> None:
    """做成窗口能播的 H.264 副本。长边不超过 1280，识别仍用原片。"""
    if cancel.is_set():
        raise FFmpegError("预览已取消")
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.stem + ".part.mp4")
    partial.unlink(missing_ok=True)
    try:
        await _transcode_preview(source, partial, has_audio=has_audio, cancel=cancel, on_process=on_process)
    except FFmpegError:
        partial.unlink(missing_ok=True)
        if cancel.is_set() or not has_audio:
            raise
        await _transcode_preview(source, partial, has_audio=False, cancel=cancel, on_process=on_process)
    if cancel.is_set():
        partial.unlink(missing_ok=True)
        raise FFmpegError("预览已取消")
    partial.replace(dest)


async def _transcode_preview(
    source: Path,
    dest: Path,
    *,
    has_audio: bool,
    cancel: asyncio.Event,
    on_process: Callable[[asyncio.subprocess.Process], None] | None,
) -> None:
    scale = await scale_filter(_PREVIEW_EDGE)
    cmd = [
        resolve_bin("ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *thread_args(),
        "-i",
        str(source),
        "-vf",
        scale,
        "-c:v",
        "libx264",
        "-profile:v",
        "baseline",  # 使用 Baseline Profile 确保 WebView2 兼容性
        "-level",
        "3.0",       # Level 3.0 适合大多数设备
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",  # 将 moov atom 移到文件开头，支持流式播放
    ]
    if has_audio:
        cmd.extend(["-c:a", "aac", "-b:a", "128k"])
    else:
        cmd.append("-an")
    cmd.extend([*thread_args(), str(dest)])
    if cancel.is_set():
        raise FFmpegError("预览已取消")
    proc = await spawn_process(*cmd, stdout=asyncio.subprocess.DEVNULL)
    if on_process is not None:
        on_process(proc)
    if cancel.is_set():
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise FFmpegError("预览已取消")

    async def _stop_when_cancelled() -> None:
        await cancel.wait()
        if proc.returncode is None:
            proc.kill()

    watcher = asyncio.create_task(_stop_when_cancelled())
    try:
        _, stderr = await proc.communicate()
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        raise
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
    if cancel.is_set():
        raise FFmpegError("预览已取消")
    if proc.returncode != 0:
        detail = decode_bytes(stderr).strip()
        raise FFmpegError(detail or "预览转码失败")


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


def frame_bounds(index: int, fps: float, duration: float) -> tuple[float, float] | None:
    """第 index 帧（从 1 开始）在视频里的起止秒。超出时长则没有这一帧。"""
    if index < 1 or fps <= 0:
        return None
    start = (index - 1) / fps
    if duration > 0 and start >= duration - 1e-3:
        return None
    end = index / fps
    if duration > 0:
        end = min(end, duration)
    if end <= start:
        end = start + (1 / fps)
    return round(start, 3), round(end, 3)


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
        bounds = frame_bounds(index, fps, duration)
        if bounds is None:
            continue
        start, end = bounds
        frames.append(Frame(index=index, start_sec=start, end_sec=end, path=str(path)))
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
    vf = f"{sample_filter(fps)},{await scale_filter(max_edge)}"
    pattern = str(directory / "frame_%06d.jpg")
    cmd = [
        resolve_bin("ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        *thread_args(),
        "-i",
        str(video),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        vf,
        *await vfr_args(),
        "-q:v",
        str(int(jpeg_q)),
        "-start_number",
        "1",
        *thread_args(),
        pattern,
    ]
    proc = await spawn_process(*cmd, stdout=asyncio.subprocess.DEVNULL)
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


async def extract_one_frame(
    video: Path,
    dest: Path,
    *,
    index: int,
    fps: float,
    duration: float,
    max_edge: int,
    jpeg_q: int,
    cancel: asyncio.Event,
    on_process: Callable[[asyncio.subprocess.Process | None], None] | None = None,
    at_sec: float | None = None,
) -> Frame:
    """按原来的帧号重抽一张。取消时删掉半成品。

    at_sec 用来重抽闪帧：帧号不在 0.5–2 FPS 的网格上，按真实时间定位。
    """
    if at_sec is not None:
        start = max(0.0, float(at_sec))
        end = start
        if duration > 0 and start >= duration:
            raise FFmpegError("这一帧超出视频时长")
    else:
        bounds = frame_bounds(index, fps, duration)
        if bounds is None:
            raise FFmpegError("这一帧超出视频时长")
        start, end = bounds
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    vf = await scale_filter(max_edge)
    cmd = [
        resolve_bin("ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-ss",
        f"{start:.3f}",
        *thread_args(),
        "-i",
        str(video),
        "-map",
        "0:v:0",
        "-an",
        "-frames:v",
        "1",
        "-vf",
        vf,
        "-q:v",
        str(int(jpeg_q)),
        *thread_args(),
        str(dest),
    ]
    if cancel.is_set():
        raise FFmpegError("已取消")
    proc = await spawn_process(*cmd, stdout=asyncio.subprocess.DEVNULL)
    if on_process:
        on_process(proc)
    try:
        stderr = await proc.stderr.read() if proc.stderr is not None else b""
        code = await proc.wait()
        if cancel.is_set():
            raise FFmpegError("已取消")
        if code != 0 or not dest.is_file() or dest.stat().st_size <= 0:
            detail = decode_bytes(stderr).strip()
            raise FFmpegError(detail or f"ffmpeg 抽帧失败，退出码 {code}")
    finally:
        if proc.returncode is None:
            proc.kill()
            try:
                await proc.wait()
            except ProcessLookupError:
                pass
        if on_process:
            on_process(None)
        if cancel.is_set() and dest.exists():
            dest.unlink(missing_ok=True)
    return Frame(index=index, start_sec=start, end_sec=end, path=str(dest))


async def _read_exact(stream: asyncio.StreamReader, size: int, cancel: asyncio.Event) -> bytes:
    chunks: list[bytes] = []
    got = 0
    while got < size:
        if cancel.is_set():
            return b""
        try:
            piece = await asyncio.wait_for(stream.read(size - got), timeout=0.3)
        except TimeoutError:
            continue
        if not piece:
            break
        chunks.append(piece)
        got += len(piece)
    return b"".join(chunks)


async def _iter_gray_frames(
    video: Path,
    width: int,
    height: int,
    cancel: asyncio.Event,
    times: list[float],
) -> AsyncIterator[bytes]:
    """按原始帧率吐出灰度图。时间戳从 showinfo 读，读不齐就留给调用方用帧率推算。"""
    frame_bytes = width * height
    cmd = [
        resolve_bin("ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostdin",
        *thread_args(),
        "-i",
        str(video),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        f"scale={width}:{height}:flags=fast_bilinear,showinfo",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        *thread_args(),
        "pipe:1",
    ]
    proc = await spawn_process(*cmd)
    assert proc.stdout is not None
    assert proc.stderr is not None

    async def read_stderr() -> None:
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            for match in _PTS_TIME.finditer(decode_bytes(line)):
                try:
                    times.append(float(match.group(1)))
                except ValueError:
                    continue

    reader = asyncio.create_task(read_stderr())
    try:
        while not cancel.is_set():
            frame = await _read_exact(proc.stdout, frame_bytes, cancel)
            if len(frame) != frame_bytes:
                break
            yield frame
    finally:
        if proc.returncode is None:
            proc.kill()
            try:
                await proc.wait()
            except ProcessLookupError:
                pass
        await reader


async def scan_flashes(
    video: Path,
    *,
    width: int,
    height: int,
    fps: float,
    duration: float,
    sample_fps: float,
    cancel: asyncio.Event,
    motion_enabled: bool = False,
) -> list[FlashSpan]:
    """扫完全部源帧，返回普通抽帧会漏掉的贴字时刻。扫描失败由调用方决定是否继续。

    motion_enabled 为假时只做亮度比较。任务里的「闪帧检测」关掉时不会调用这里。
    """
    scanned_w, scanned_h = scan_size(width, height)
    if scanned_w < 2 or scanned_h < 2:
        return []
    scanner = FlashScanner()
    motion: MotionScanner | None = None
    if motion_enabled and motion_available():
        try:
            import cv2

            cv2.setNumThreads(1)
            motion = MotionScanner(scanned_w, scanned_h)
        except Exception as exc:
            log.warning("运动扫描不可用，改为只按亮度比较：%s", exc)
            motion = None
    times: list[float] = []
    seen = 0
    started = time.perf_counter()

    def process_frame(frame: bytes) -> None:
        """像素计算放到线程里做，不占事件循环。逐帧顺序调用，不会并发。"""
        nonlocal motion
        scanner.push(block_means(frame, scanned_w, scanned_h))
        if motion is not None:
            try:
                motion.push(frame)
            except Exception as exc:
                log.warning("运动扫描失败，改为只按亮度比较：%s", exc)
                motion = None

    async for frame in _iter_gray_frames(video, scanned_w, scanned_h, cancel, times):
        await asyncio.to_thread(process_frame, bytes(frame))
        seen += 1
    if cancel.is_set() or seen < 3:
        return []
    if len(times) != seen:
        times = [index / fps for index in range(seen)] if fps > 0 else []
    rate = fps if fps > 0 else 30.0
    bright = spans_from_runs(
        scanner.finish(),
        times,
        fps=rate,
        duration=duration,
        sample_fps=sample_fps,
    )
    moved: list[FlashSpan] = []
    if motion is not None:
        try:
            hits = motion.finish()
            moved = stamp_crops(
                spans_from_runs(
                    motion_runs(hits),
                    times,
                    fps=rate,
                    duration=duration,
                    sample_fps=sample_fps,
                ),
                hits,
                scanned_w,
                scanned_h,
            )
        except Exception as exc:
            log.warning("运动扫描失败，改为只按亮度比较：%s", exc)
            moved = []
    merged = merge_flash_spans(moved, bright)
    log.info(
        "闪帧扫描 %.1f 秒，亮度 %d 帧，运动 %d 帧，送去识别 %d 帧",
        time.perf_counter() - started,
        len(bright),
        len(moved),
        len(merged),
    )
    return merged


async def extract_source_frames(
    video: Path,
    directory: Path,
    spans: list[FlashSpan],
    *,
    max_edge: int,
    jpeg_q: int,
    cancel: asyncio.Event,
    on_process: Callable[[asyncio.subprocess.Process | None], None] | None = None,
) -> list[Frame]:
    """按源帧号各抽一张。一次滤镜选中全部闪帧，避免逐张从片头解码。"""
    ordered = sorted(spans, key=lambda item: item.source_index)
    if not ordered or cancel.is_set():
        return []
    directory.mkdir(parents=True, exist_ok=True)
    expr = "+".join(f"eq(n\\,{item.source_index})" for item in ordered)
    vf = f"select='{expr}',{await scale_filter(max_edge)}"
    pattern = str(directory / "flash_%06d.jpg")
    cmd = [
        resolve_bin("ffmpeg"),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        *thread_args(),
        "-i",
        str(video),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        vf,
        *await vfr_args(),
        "-q:v",
        str(int(jpeg_q)),
        "-start_number",
        "1",
        *thread_args(),
        pattern,
    ]
    proc = await spawn_process(*cmd, stdout=asyncio.subprocess.DEVNULL)
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
        if cancel.is_set():
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            return []
        if proc.returncode not in (0, None):
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
    frames: list[Frame] = []
    for offset, span in enumerate(ordered, start=1):
        path = directory / f"flash_{offset:06d}.jpg"
        if not path.is_file() or path.stat().st_size <= 0:
            # 输出按顺序编号。缺一张就对不回后面的时刻，整批放弃，避免把价格标到别的时间。
            log.warning("闪帧截图和检测到的时刻对不上，这批一闪而过的画面跳过")
            return []
        if span.crop is not None:
            try:
                magnify_flash(path, span.crop)
            except Exception as exc:
                log.warning("闪帧放大失败，这帧改用整张画面：%s", exc)
        frames.append(
            Frame(
                index=flash_frame_index(span.start_sec),
                start_sec=span.start_sec,
                end_sec=span.end_sec,
                path=str(path),
            )
        )
    return frames


async def extract_strip(video: Path, directory: Path) -> int:
    """大约每秒一张、高度约 48 像素的时间轴缩略图。失败时不留半成品。"""
    directory.mkdir(parents=True, exist_ok=True)
    pattern = str(directory / "strip_%04d.jpg")
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
        f"{sample_filter(1)},scale=-2:48",
        *await vfr_args(),
        "-q:v",
        "8",
        "-start_number",
        "0",
        pattern,
    ]
    code, text = await _run(cmd)
    files = [path for path in directory.glob("strip_*.jpg") if path.stat().st_size > 0]
    if code != 0 or not files:
        for path in directory.glob("strip_*.jpg"):
            path.unlink(missing_ok=True)
        detail = text.strip() or "时间轴缩略图生成失败"
        raise FFmpegError(detail[:300])
    return len(files)


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
        *thread_args(),
        "-i",
        str(video),
    ]
    if start is not None and start > 0:
        cmd.extend(["-ss", f"{start:.3f}"])
    if duration is not None and duration > 0:
        cmd.extend(["-t", f"{duration:.3f}"])
    cmd.extend(["-vn", "-ac", "1", "-ar", "16000", *codec_args, *thread_args(), str(dest)])
    proc = await spawn_process(*cmd, stdout=asyncio.subprocess.DEVNULL)
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
