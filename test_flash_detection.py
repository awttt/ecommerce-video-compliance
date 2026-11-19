#!/usr/bin/env python3
"""闪帧检测改进验证脚本。

用于测试不同参数设置下的检测效果。
"""

import asyncio
import sys
from pathlib import Path

# 确保能找到 app 模块
sys.path.insert(0, str(Path(__file__).parent))


async def test_detection(video_path: Path, use_adaptive: bool = True):
    """测试闪帧检测。"""
    from app.core.ffmpeg import probe_video, scan_flashes
    from app.core.flash import scan_size, get_flash_params
    from app.core.motion import get_motion_params, available as motion_available

    print(f"\n{'='*60}")
    print(f"测试视频: {video_path.name}")
    print(f"自适应参数: {'启用' if use_adaptive else '禁用'}")
    print(f"{'='*60}\n")

    # 探测视频
    probe = await probe_video(video_path)
    print(f"视频信息:")
    print(f"  分辨率: {probe.width}x{probe.height}")
    print(f"  帧率: {probe.fps:.1f} FPS")
    print(f"  时长: {probe.duration:.1f} 秒")
    print(f"  编码: {probe.codec}")

    scan_w, scan_h = scan_size(probe.width, probe.height)
    print(f"  扫描尺寸: {scan_w}x{scan_h}\n")

    # 检测环境
    print(f"检测环境:")
    print(f"  运动扫描: {'可用' if motion_available() else '不可用'}")

    # 执行检测
    cancel = asyncio.Event()
    sample_fps = 1.0

    try:
        spans = await scan_flashes(
            video_path,
            width=probe.width,
            height=probe.height,
            fps=probe.fps,
            duration=probe.duration,
            sample_fps=sample_fps,
            cancel=cancel,
            motion_enabled=True,
            adaptive_params=use_adaptive,
        )

        # 显示参数
        flash_change, flash_max_run = get_flash_params()
        print(f"\n使用的参数:")
        print(f"  亮度阈值: {flash_change}")
        print(f"  最大连续帧: {flash_max_run}")

        if motion_available():
            motion_residual, motion_min_score = get_motion_params()
            print(f"  运动残差阈值: {motion_residual}")
            print(f"  运动最小分数: {motion_min_score}")

        # 显示结果
        print(f"\n检测结果:")
        print(f"  检测到闪帧: {len(spans)} 个")
        if spans:
            print(f"\n  前 5 个闪帧:")
            for i, span in enumerate(spans[:5], 1):
                print(f"    {i}. {span.start_sec:.2f}s - {span.end_sec:.2f}s, 分数: {span.score}")

        density = (len(spans) / probe.duration * 60) if probe.duration > 0 else 0
        print(f"\n  检测密度: {density:.2f} 个/分钟")

    except Exception as exc:
        print(f"\n❌ 检测失败: {exc}")
        import traceback
        traceback.print_exc()
        return False

    return True


async def compare_modes(video_path: Path):
    """对比启用和禁用自适应参数的效果。"""
    print("\n" + "="*60)
    print("对比测试：自适应参数 vs 默认参数")
    print("="*60)

    print("\n【测试 1：使用自适应参数】")
    await test_detection(video_path, use_adaptive=True)

    print("\n\n【测试 2：使用默认参数】")
    await test_detection(video_path, use_adaptive=False)


def main():
    """主函数。"""
    if len(sys.argv) < 2:
        print("用法: python test_flash_detection.py <视频路径> [--compare]")
        print("\n选项:")
        print("  --compare  对比启用和禁用自适应参数的效果")
        sys.exit(1)

    video_path = Path(sys.argv[1])
    if not video_path.is_file():
        print(f"错误: 找不到视频文件 {video_path}")
        sys.exit(1)

    compare = "--compare" in sys.argv

    if compare:
        asyncio.run(compare_modes(video_path))
    else:
        asyncio.run(test_detection(video_path, use_adaptive=True))


if __name__ == "__main__":
    main()
