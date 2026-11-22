#!/usr/bin/env python3
"""闪帧检测使用示例。

演示如何使用改进后的闪帧检测功能。
"""

import asyncio
from pathlib import Path


async def example_basic_detection():
    """基础示例：自动检测闪帧（使用自适应参数）"""
    from app.core.ffmpeg import probe_video, scan_flashes

    video_path = Path("test_video.mp4")

    # 1. 探测视频信息
    probe = await probe_video(video_path)
    print(f"视频: {probe.width}x{probe.height}, {probe.fps:.1f} FPS, {probe.duration:.1f}s")

    # 2. 扫描闪帧（自动使用自适应参数）
    cancel = asyncio.Event()
    spans = await scan_flashes(
        video_path,
        width=probe.width,
        height=probe.height,
        fps=probe.fps,
        duration=probe.duration,
        sample_fps=1.0,
        cancel=cancel,
        motion_enabled=True,  # 启用运动检测
        adaptive_params=True,  # 启用自适应参数（默认）
    )

    print(f"检测到 {len(spans)} 个闪帧")
    for span in spans[:5]:
        print(f"  {span.start_sec:.2f}s - {span.end_sec:.2f}s")


async def example_custom_params():
    """高级示例：自定义检测参数"""
    from app.core.ffmpeg import probe_video, scan_flashes
    from app.core.flash import set_flash_params
    from app.core.motion import set_motion_params

    video_path = Path("test_video.mp4")
    probe = await probe_video(video_path)

    # 手动设置更敏感的参数
    set_flash_params(
        change_threshold=18,  # 降低亮度阈值（默认 24）
        max_run=4,            # 允许更长的连续帧（默认 3）
    )
    set_motion_params(
        residual_threshold=35,  # 降低运动残差阈值（默认 45）
        min_score=50,           # 降低最小分数（默认 60）
    )

    # 扫描（禁用自适应，使用上面的自定义参数）
    cancel = asyncio.Event()
    spans = await scan_flashes(
        video_path,
        width=probe.width,
        height=probe.height,
        fps=probe.fps,
        duration=probe.duration,
        sample_fps=1.0,
        cancel=cancel,
        motion_enabled=True,
        adaptive_params=False,  # 禁用自适应，使用自定义参数
    )

    print(f"使用自定义参数检测到 {len(spans)} 个闪帧")


async def example_diagnostic():
    """诊断示例：检查环境和视频"""
    from app.core.flash_diagnostics import diagnose_environment, diagnose_video

    # 1. 诊断运行环境
    env = diagnose_environment()
    print("运行环境:")
    print(f"  OpenCV: {env['has_opencv']}")
    print(f"  NumPy: {env['has_numpy']}")
    print(f"  运动扫描: {env['motion_available']}")

    if env['issues']:
        print("\n问题:")
        for issue in env['issues']:
            print(f"  - {issue}")

    if env['suggestions']:
        print("\n建议:")
        for suggestion in env['suggestions']:
            print(f"  - {suggestion}")

    # 2. 诊断视频文件
    video_path = Path("test_video.mp4")
    if video_path.exists():
        video = diagnose_video(video_path)
        print(f"\n视频 {video_path.name}:")
        if video['video_readable']:
            print(f"  分辨率: {video['width']}x{video['height']}")
            print(f"  帧率: {video['fps']:.1f} FPS")
            print(f"  扫描尺寸: {video['scan_width']}x{video['scan_height']}")

            if video['issues']:
                print("\n  问题:")
                for issue in video['issues']:
                    print(f"    - {issue}")
        else:
            print(f"  错误: {video['error']}")


async def example_compare_modes():
    """对比示例：自适应 vs 默认参数"""
    from app.core.ffmpeg import probe_video, scan_flashes

    video_path = Path("test_video.mp4")
    probe = await probe_video(video_path)
    cancel = asyncio.Event()

    # 使用自适应参数
    spans_adaptive = await scan_flashes(
        video_path,
        width=probe.width,
        height=probe.height,
        fps=probe.fps,
        duration=probe.duration,
        sample_fps=1.0,
        cancel=cancel,
        motion_enabled=True,
        adaptive_params=True,
    )

    # 使用默认参数
    spans_default = await scan_flashes(
        video_path,
        width=probe.width,
        height=probe.height,
        fps=probe.fps,
        duration=probe.duration,
        sample_fps=1.0,
        cancel=cancel,
        motion_enabled=True,
        adaptive_params=False,
    )

    print(f"自适应参数: {len(spans_adaptive)} 个闪帧")
    print(f"默认参数: {len(spans_default)} 个闪帧")
    print(f"差异: {abs(len(spans_adaptive) - len(spans_default))} 个")


def main():
    """运行示例"""
    print("=== 示例 1: 基础检测 ===\n")
    # asyncio.run(example_basic_detection())

    print("\n=== 示例 2: 自定义参数 ===\n")
    # asyncio.run(example_custom_params())

    print("\n=== 示例 3: 环境诊断 ===\n")
    asyncio.run(example_diagnostic())

    print("\n=== 示例 4: 对比测试 ===\n")
    # asyncio.run(example_compare_modes())


if __name__ == "__main__":
    main()
