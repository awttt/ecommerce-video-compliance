#!/usr/bin/env python3
"""诊断为什么某个闪帧没被检测到。

用于分析特定时间点的视频帧，查看检测参数是否合理。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


async def analyze_flash_at_time(video_path: Path, target_sec: float):
    """分析特定时间点附近的帧。"""
    from app.core.ffmpeg import probe_video, scan_flashes
    from app.core.flash import (
        block_means,
        get_flash_params,
        set_flash_params,
        SCAN_EDGE,
        scan_size,
    )

    print("="*70)
    print(f"分析视频: {video_path.name}")
    print(f"目标时间: {target_sec:.2f} 秒")
    print("="*70)

    # 1. 探测视频
    probe = await probe_video(video_path)
    print(f"\n视频信息:")
    print(f"  分辨率: {probe.width}x{probe.height}")
    print(f"  帧率: {probe.fps:.2f} FPS")
    print(f"  时长: {probe.duration:.1f}秒")

    scan_w, scan_h = scan_size(probe.width, probe.height)
    print(f"  扫描尺寸: {scan_w}x{scan_h}")

    # 2. 当前检测参数
    change_threshold, max_run = get_flash_params()
    print(f"\n当前检测参数:")
    print(f"  亮度阈值 (CHANGE): {change_threshold}")
    print(f"  最大连续帧 (MAX_RUN): {max_run}")

    # 3. 计算目标帧号
    target_frame = int(target_sec * probe.fps)
    print(f"\n目标帧号: {target_frame} (第 {target_sec:.2f} 秒)")

    # 4. 运行闪帧检测
    print(f"\n正在扫描闪帧...")
    cancel = asyncio.Event()

    spans = await scan_flashes(
        video_path,
        width=probe.width,
        height=probe.height,
        fps=probe.fps,
        duration=probe.duration,
        sample_fps=1.0,  # 使用 1 FPS 普通抽帧
        cancel=cancel,
        motion_enabled=True,
        adaptive_params=True,
    )

    print(f"检测到 {len(spans)} 个闪帧")

    # 5. 查找目标时间附近的检测结果
    found_near_target = False
    print(f"\n目标时间 {target_sec:.2f}s 附近的闪帧:")

    for span in spans:
        if abs(span.start_sec - target_sec) <= 1.0:  # 前后1秒范围
            found_near_target = True
            print(f"  ✓ {span.start_sec:.2f}s - {span.end_sec:.2f}s, 分数: {span.score}, 源帧: {span.source_index}")

    if not found_near_target:
        print(f"  ✗ 没有检测到目标时间附近的闪帧")

    # 6. 普通抽帧覆盖情况
    print(f"\n普通抽帧 (1 FPS) 覆盖:")
    sample_frames = []
    for i in range(int(probe.duration) + 1):
        if abs(i - target_sec) <= 2:  # 前后2秒
            sample_frames.append(i)
            marker = " ← 目标" if abs(i - target_sec) < 0.5 else ""
            print(f"  第 {i} 秒{marker}")

    # 7. 建议
    print(f"\n诊断建议:")

    if not found_near_target:
        print(f"  ⚠️  目标闪帧未被检测到")
        print(f"\n  可能原因:")
        print(f"    1. 闪帧持续时间太短（<3帧），普通抽帧和闪帧检测都漏掉")
        print(f"    2. 亮度变化不够明显，未达到阈值 {change_threshold}")
        print(f"    3. 画面持续运动，运动扫描残差被干扰")
        print(f"    4. 字幕太小或对比度太低")

        print(f"\n  建议尝试:")
        print(f"    1. 降低亮度阈值:")
        print(f"       from app.core.flash import set_flash_params")
        print(f"       set_flash_params(change_threshold={change_threshold - 6}, max_run={max_run})")

        print(f"\n    2. 降低运动检测阈值:")
        print(f"       from app.core.motion import set_motion_params")
        print(f"       set_motion_params(residual_threshold=35, min_score=45)")

        print(f"\n    3. 提高抽帧密度:")
        print(f"       在任务设置中使用 2 FPS（默认是 1 FPS）")

        print(f"\n    4. 手动查看:")
        print(f"       ffmpeg -ss {target_sec:.2f} -i \"{video_path}\" -frames:v 3 frame_%d.jpg")

    else:
        print(f"  ✓ 目标闪帧已被检测到")


async def compare_with_different_thresholds(video_path: Path, target_sec: float):
    """对比不同阈值的检测效果。"""
    from app.core.ffmpeg import probe_video, scan_flashes
    from app.core.flash import set_flash_params
    from app.core.motion import set_motion_params

    print("\n" + "="*70)
    print("对比不同阈值的检测效果")
    print("="*70)

    probe = await probe_video(video_path)
    cancel = asyncio.Event()

    thresholds = [
        {"change": 24, "residual": 45, "min_score": 60, "name": "默认"},
        {"change": 18, "residual": 35, "min_score": 50, "name": "较低"},
        {"change": 30, "residual": 55, "min_score": 70, "name": "较高"},
    ]

    results = []

    for params in thresholds:
        print(f"\n测试参数: {params['name']}")
        print(f"  change={params['change']}, residual={params['residual']}, min_score={params['min_score']}")

        # 设置参数
        set_flash_params(change_threshold=params['change'])
        set_motion_params(residual_threshold=params['residual'], min_score=params['min_score'])

        # 扫描
        spans = await scan_flashes(
            video_path,
            width=probe.width,
            height=probe.height,
            fps=probe.fps,
            duration=probe.duration,
            sample_fps=1.0,
            cancel=cancel,
            motion_enabled=True,
            adaptive_params=False,  # 禁用自适应，使用指定参数
        )

        # 检查目标时间
        found = False
        for span in spans:
            if abs(span.start_sec - target_sec) <= 1.0:
                found = True
                break

        result = {
            "name": params['name'],
            "total": len(spans),
            "found_target": found,
        }
        results.append(result)

        status = "✓" if found else "✗"
        print(f"  {status} 检测到 {len(spans)} 个闪帧, 目标: {'找到' if found else '未找到'}")

    # 总结
    print("\n" + "="*70)
    print("对比总结")
    print("="*70)
    print(f"{'阈值设置':<15} {'总数':<10} {'目标':<10}")
    print("-"*70)
    for r in results:
        status = "✓ 找到" if r['found_target'] else "✗ 未找到"
        print(f"{r['name']:<15} {r['total']:<10} {status}")


async def extract_frames_around_time(video_path: Path, target_sec: float, before: float = 0.5, after: float = 0.5):
    """提取目标时间前后的帧。"""
    print("\n" + "="*70)
    print(f"提取 {target_sec:.2f}s 前后的帧")
    print("="*70)

    output_dir = Path("./flash_debug_frames")
    output_dir.mkdir(exist_ok=True)

    start_time = max(0, target_sec - before)
    duration = before + after

    cmd = [
        "ffmpeg",
        "-ss", f"{start_time:.3f}",
        "-i", str(video_path),
        "-t", f"{duration:.3f}",
        "-vf", "scale=1280:1280:force_original_aspect_ratio=decrease",
        "-q:v", "2",
        str(output_dir / "frame_%04d.jpg"),
        "-y"
    ]

    import subprocess
    result = subprocess.run(cmd, capture_output=True)

    if result.returncode == 0:
        frames = list(output_dir.glob("frame_*.jpg"))
        print(f"\n✓ 提取了 {len(frames)} 帧")
        print(f"  保存目录: {output_dir}")
        print(f"\n  时间范围: {start_time:.3f}s - {start_time + duration:.3f}s")
        print(f"  建议手动检查这些帧，确认价格字幕是否出现")
    else:
        print(f"\n✗ 提取失败: {result.stderr.decode()}")


async def main():
    """主函数。"""
    import argparse

    parser = argparse.ArgumentParser(description="诊断闪帧检测遗漏")
    parser.add_argument("video", type=Path, help="视频文件路径")
    parser.add_argument("--time", type=float, required=True, help="目标时间（秒）")
    parser.add_argument("--compare", action="store_true", help="对比不同阈值")
    parser.add_argument("--extract", action="store_true", help="提取目标时间附近的帧")

    args = parser.parse_args()

    if not args.video.exists():
        print(f"错误: 视频文件不存在 {args.video}")
        return 1

    # 基本分析
    await analyze_flash_at_time(args.video, args.time)

    # 对比不同阈值
    if args.compare:
        await compare_with_different_thresholds(args.video, args.time)

    # 提取帧
    if args.extract:
        await extract_frames_around_time(args.video, args.time)


if __name__ == "__main__":
    asyncio.run(main())
