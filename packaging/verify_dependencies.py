#!/usr/bin/env python3
"""验证打包后的程序是否包含所需的依赖。

在打包后的 dist/VideoCompliance 目录中运行此脚本，检查依赖是否正确包含。
"""

import sys
from pathlib import Path


def verify_opencv():
    """验证 OpenCV 是否可用。"""
    print("检查 OpenCV...")
    try:
        import cv2
        version = cv2.__version__
        print(f"  ✓ OpenCV {version} 可用")

        # 测试基本功能
        import numpy as np
        test_img = np.zeros((100, 100), dtype=np.uint8)
        _ = cv2.GaussianBlur(test_img, (5, 5), 0)
        print("  ✓ OpenCV 基本功能正常")

        return True
    except ImportError as e:
        print(f"  ✗ OpenCV 不可用: {e}")
        return False
    except Exception as e:
        print(f"  ✗ OpenCV 功能测试失败: {e}")
        return False


def verify_numpy():
    """验证 NumPy 是否可用。"""
    print("\n检查 NumPy...")
    try:
        import numpy as np
        version = np.__version__
        print(f"  ✓ NumPy {version} 可用")

        # 测试基本功能
        arr = np.array([1, 2, 3, 4, 5])
        _ = np.mean(arr)
        _ = np.std(arr)
        print("  ✓ NumPy 基本功能正常")

        return True
    except ImportError as e:
        print(f"  ✗ NumPy 不可用: {e}")
        return False
    except Exception as e:
        print(f"  ✗ NumPy 功能测试失败: {e}")
        return False


def verify_motion_scanner():
    """验证运动扫描功能是否可用。"""
    print("\n检查运动扫描功能...")
    try:
        from app.core.motion import available, MotionScanner

        if not available():
            print("  ✗ 运动扫描不可用（缺少依赖）")
            return False

        print("  ✓ 运动扫描依赖可用")

        # 测试创建 MotionScanner
        scanner = MotionScanner(640, 360)
        print("  ✓ MotionScanner 可以创建")

        return True
    except Exception as e:
        print(f"  ✗ 运动扫描功能测试失败: {e}")
        return False


def verify_flash_detection():
    """验证闪帧检测功能是否可用。"""
    print("\n检查闪帧检测功能...")
    try:
        from app.core.flash import FlashScanner, block_means

        scanner = FlashScanner()
        print("  ✓ FlashScanner 可以创建")

        # 测试 block_means
        import numpy as np
        test_frame = np.random.randint(0, 256, 640 * 360, dtype=np.uint8).tobytes()
        means = block_means(test_frame, 640, 360)
        print(f"  ✓ block_means 可以运行（输出 {len(means)} 个值）")

        return True
    except Exception as e:
        print(f"  ✗ 闪帧检测功能测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """主函数。"""
    print("="*60)
    print("依赖验证")
    print("="*60)
    print(f"Python 版本: {sys.version}")
    print(f"运行目录: {Path.cwd()}")
    print(f"可执行文件: {sys.executable}")
    print("="*60 + "\n")

    results = {
        "NumPy": verify_numpy(),
        "OpenCV": verify_opencv(),
        "运动扫描": verify_motion_scanner(),
        "闪帧检测": verify_flash_detection(),
    }

    print("\n" + "="*60)
    print("验证结果摘要")
    print("="*60)

    all_passed = True
    for name, passed in results.items():
        status = "✓ 通过" if passed else "✗ 失败"
        print(f"{name:12s}: {status}")
        if not passed:
            all_passed = False

    print("="*60)

    if all_passed:
        print("\n✓ 所有依赖验证通过！")
        return 0
    else:
        print("\n✗ 部分依赖验证失败，请检查打包配置。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
