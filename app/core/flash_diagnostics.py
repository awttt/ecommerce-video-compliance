"""闪帧检测诊断工具。

帮助用户诊断为什么检测不到闪帧，并提供针对性建议。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class DiagnosticResult:
    """诊断结果。"""
    has_opencv: bool
    has_numpy: bool
    motion_available: bool
    video_readable: bool
    video_width: int
    video_height: int
    video_duration: float
    video_fps: float
    scan_width: int
    scan_height: int
    issues: list[str]
    suggestions: list[str]


def diagnose_environment() -> dict[str, object]:
    """诊断运行环境。"""
    issues = []
    suggestions = []

    # 检查依赖
    try:
        import cv2
        opencv_version = cv2.__version__
        has_opencv = True
    except ImportError:
        opencv_version = None
        has_opencv = False
        issues.append("缺少 opencv-python 库")
        suggestions.append("安装 opencv-python：pip install opencv-python")

    try:
        import numpy as np
        numpy_version = np.__version__
        has_numpy = True
    except ImportError:
        numpy_version = None
        has_numpy = False
        issues.append("缺少 numpy 库")
        suggestions.append("安装 numpy：pip install numpy")

    from app.core.motion import available as motion_available

    if not motion_available():
        issues.append("运动扫描不可用，只能使用亮度比较")
        if not has_opencv or not has_numpy:
            suggestions.append("安装 opencv 和 numpy 以启用运动扫描，提升检测准确性")

    return {
        "has_opencv": has_opencv,
        "opencv_version": opencv_version,
        "has_numpy": has_numpy,
        "numpy_version": numpy_version,
        "motion_available": motion_available(),
        "issues": issues,
        "suggestions": suggestions,
    }


def diagnose_video(video_path: Path) -> dict[str, object]:
    """诊断视频文件。"""
    from app.core.ffmpeg import probe_video, FFmpegError
    from app.core.flash import scan_size

    issues = []
    suggestions = []

    try:
        import asyncio
        probe = asyncio.run(probe_video(video_path))
    except FFmpegError as exc:
        return {
            "video_readable": False,
            "error": str(exc),
            "issues": ["视频文件无法读取"],
            "suggestions": ["检查视频文件是否损坏", "确认已安装 FFmpeg"],
        }

    # 检查视频属性
    if probe.width < 320 or probe.height < 180:
        issues.append(f"视频分辨率过低：{probe.width}x{probe.height}")
        suggestions.append("低分辨率视频检测效果可能不佳，建议使用更高分辨率的源视频")

    if probe.fps < 15:
        issues.append(f"视频帧率过低：{probe.fps:.1f} FPS")
        suggestions.append("低帧率视频可能导致闪帧检测遗漏")
    elif probe.fps > 60:
        issues.append(f"视频帧率很高：{probe.fps:.1f} FPS")
        suggestions.append("高帧率视频扫描时间较长，考虑降低扫描帧率")

    if probe.duration < 1:
        issues.append("视频时长过短")

    # 检查扫描尺寸
    scan_w, scan_h = scan_size(probe.width, probe.height)
    if scan_w < 320 or scan_h < 180:
        issues.append(f"扫描尺寸过小：{scan_w}x{scan_h}")
        suggestions.append("视频缩放后特征不明显，建议使用原始高分辨率视频")

    # 编码检查
    if probe.codec.lower() in {"mjpeg", "png", "bmp"}:
        suggestions.append(f"视频编码 {probe.codec} 可能不适合检测，建议使用 H.264 或 VP9")

    return {
        "video_readable": True,
        "width": probe.width,
        "height": probe.height,
        "duration": probe.duration,
        "fps": probe.fps,
        "codec": probe.codec,
        "scan_width": scan_w,
        "scan_height": scan_h,
        "issues": issues,
        "suggestions": suggestions,
    }


def print_diagnostic_report(video_path: Path | None = None) -> None:
    """打印完整诊断报告。"""
    print("\n" + "=" * 60)
    print("闪帧检测诊断报告")
    print("=" * 60 + "\n")

    # 环境诊断
    print("【运行环境】")
    env = diagnose_environment()
    print(f"  OpenCV: {'已安装 ' + env['opencv_version'] if env['has_opencv'] else '未安装'}")
    print(f"  NumPy: {'已安装 ' + env['numpy_version'] if env['has_numpy'] else '未安装'}")
    print(f"  运动扫描: {'可用' if env['motion_available'] else '不可用'}")

    if env['issues']:
        print("\n  ⚠️  问题：")
        for issue in env['issues']:
            print(f"    - {issue}")

    if env['suggestions']:
        print("\n  💡 建议：")
        for suggestion in env['suggestions']:
            print(f"    - {suggestion}")

    # 视频诊断
    if video_path:
        print("\n【视频文件】")
        print(f"  路径: {video_path}")
        video = diagnose_video(video_path)

        if not video['video_readable']:
            print(f"  ❌ 错误: {video['error']}")
        else:
            print(f"  分辨率: {video['width']}x{video['height']}")
            print(f"  时长: {video['duration']:.1f} 秒")
            print(f"  帧率: {video['fps']:.1f} FPS")
            print(f"  编码: {video['codec']}")
            print(f"  扫描尺寸: {video['scan_width']}x{video['scan_height']}")

        if video.get('issues'):
            print("\n  ⚠️  问题：")
            for issue in video['issues']:
                print(f"    - {issue}")

        if video.get('suggestions'):
            print("\n  💡 建议：")
            for suggestion in video['suggestions']:
                print(f"    - {suggestion}")

    print("\n" + "=" * 60 + "\n")


def log_detection_summary(
    detected_bright: int,
    detected_motion: int,
    merged_count: int,
    scan_duration: float,
    video_duration: float,
) -> None:
    """记录检测摘要，帮助判断检测质量。"""
    density = (merged_count / video_duration * 60) if video_duration > 0 else 0

    log.info("=" * 60)
    log.info("闪帧检测摘要")
    log.info("-" * 60)
    log.info("亮度检测: %d 帧", detected_bright)
    log.info("运动检测: %d 帧", detected_motion)
    log.info("合并后: %d 帧", merged_count)
    log.info("扫描耗时: %.1f 秒", scan_duration)
    log.info("检测密度: %.2f 个/分钟", density)

    # 给出质量判断
    if merged_count == 0:
        log.warning("⚠️  未检测到任何闪帧")
        log.warning("可能原因：")
        log.warning("  1. 视频中确实没有闪帧")
        log.warning("  2. 检测阈值过高")
        log.warning("  3. 视频压缩导致特征不明显")
        log.warning("  4. 运动扫描未启用")
    elif density > 20:
        log.warning("⚠️  检测密度过高（%.2f 个/分钟）", density)
        log.warning("可能原因：")
        log.warning("  1. 检测阈值过低")
        log.warning("  2. 视频噪声较大")
        log.warning("  3. 场景切换频繁")
    elif density < 0.1 and video_duration > 20:
        log.warning("⚠️  检测密度较低（%.2f 个/分钟）", density)
        log.info("如果预期有更多闪帧，考虑降低检测阈值")
    else:
        log.info("✓ 检测结果看起来合理")

    log.info("=" * 60)
