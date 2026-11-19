#!/usr/bin/env python3
"""测试视频播放兼容性修复。

验证不同 Profile 的视频是否被正确处理。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


async def test_profile_detection():
    """测试 Profile 检测。"""
    from app.core.ffmpeg import probe_video, window_can_play

    # 测试视频列表
    videos = [
        Path("~/Downloads/ba02aecad53a24daba4ecb43bd85168e.mp4").expanduser(),
        Path("~/Downloads/13452a57a6e66856f8151b6d38c8a4b0.mp4").expanduser(),
        Path("~/Downloads/1928ecca04ef28e91fe7af5b2a0d3cc7.mp4").expanduser(),
    ]

    print("="*70)
    print("视频播放兼容性测试")
    print("="*70)

    for video in videos:
        if not video.exists():
            print(f"\n⚠️  跳过: {video.name}（文件不存在）")
            continue

        print(f"\n视频: {video.name}")
        print("-"*70)

        try:
            probe = await probe_video(video)

            print(f"  编码: {probe.codec}")
            print(f"  Profile: {probe.profile}")
            print(f"  像素格式: {probe.pix_fmt}")
            print(f"  分辨率: {probe.width}x{probe.height}")
            print(f"  时长: {probe.duration:.1f}秒")

            can_play = window_can_play(video, probe)

            if can_play:
                print(f"\n  ✅ 可以直接播放")
            else:
                print(f"\n  ⚠️  需要转码")
                print(f"     原因: {probe.codec.upper()} {probe.profile} Profile")
                print(f"     转码为: H.264 Baseline Profile")

            # 分析为什么需要转码
            if probe.codec.lower() == "h264":
                profile_lower = probe.profile.lower()
                if "baseline" in profile_lower or "constrained" in profile_lower:
                    print(f"     说明: {probe.profile} Profile 兼容性最好")
                elif "main" in profile_lower:
                    print(f"     说明: Main Profile 在无硬件加速时可能黑屏")
                elif "high" in profile_lower:
                    print(f"     说明: High Profile 兼容性较差")

        except Exception as e:
            print(f"  ❌ 错误: {e}")

    print("\n" + "="*70)
    print("测试完成")
    print("="*70)


async def test_transcode_command():
    """显示转码命令示例。"""
    print("\n" + "="*70)
    print("转码命令示例")
    print("="*70)

    cmd = """
ffmpeg -i input.mp4 \\
  -vf "scale=1280:1280:force_original_aspect_ratio=decrease" \\
  -c:v libx264 \\
  -profile:v baseline \\
  -level 3.0 \\
  -preset veryfast \\
  -crf 23 \\
  -pix_fmt yuv420p \\
  -movflags +faststart \\
  -c:a aac \\
  -b:a 128k \\
  output.mp4
"""

    print(cmd)

    print("\n参数说明:")
    print("  -profile:v baseline  : 使用 Baseline Profile（最佳兼容性）")
    print("  -level 3.0           : Level 3.0（适合大多数设备）")
    print("  -preset veryfast     : 快速编码")
    print("  -crf 23              : 视觉无损质量")
    print("  -movflags +faststart : 支持流式播放")


def show_profile_comparison():
    """显示不同 Profile 的对比。"""
    print("\n" + "="*70)
    print("H.264 Profile 对比")
    print("="*70)

    profiles = [
        {
            "name": "Baseline",
            "compatibility": "100%",
            "webview2": "✅ 完美",
            "hardware_accel": "不需要",
            "notes": "最佳兼容性，所有设备都支持",
        },
        {
            "name": "Constrained Baseline",
            "compatibility": "100%",
            "webview2": "✅ 完美",
            "hardware_accel": "不需要",
            "notes": "移动设备优化版本",
        },
        {
            "name": "Main",
            "compatibility": "70%",
            "webview2": "⚠️ 不稳定",
            "hardware_accel": "建议启用",
            "notes": "无硬件加速可能黑屏",
        },
        {
            "name": "High",
            "compatibility": "50%",
            "webview2": "⚠️ 不稳定",
            "hardware_accel": "必须启用",
            "notes": "高级特性，兼容性差",
        },
        {
            "name": "High 10",
            "compatibility": "20%",
            "webview2": "❌ 不支持",
            "hardware_accel": "必须启用",
            "notes": "10-bit 色深，浏览器支持差",
        },
    ]

    print(f"\n{'Profile':<20} {'兼容性':<10} {'WebView2':<15} {'硬件加速':<12} 说明")
    print("-"*70)

    for p in profiles:
        print(f"{p['name']:<20} {p['compatibility']:<10} {p['webview2']:<15} {p['hardware_accel']:<12} {p['notes']}")

    print("\n推荐：统一转码为 Baseline Profile，确保所有用户都能正常播放。")


async def main():
    """主函数。"""
    import argparse

    parser = argparse.ArgumentParser(description="测试视频播放兼容性")
    parser.add_argument("--profile-info", action="store_true", help="显示 Profile 对比信息")
    parser.add_argument("--transcode-cmd", action="store_true", help="显示转码命令示例")

    args = parser.parse_args()

    if args.profile_info:
        show_profile_comparison()
        return

    if args.transcode_cmd:
        await test_transcode_command()
        return

    # 默认：运行兼容性测试
    await test_profile_detection()


if __name__ == "__main__":
    asyncio.run(main())
