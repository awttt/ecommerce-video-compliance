#!/usr/bin/env python3
"""验证闪帧检测参数是否生效。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def verify_params():
    """验证参数。"""
    print("="*60)
    print("验证闪帧检测参数")
    print("="*60)

    # 1. 检查常量
    from app.core import flash, motion

    print("\n【常量值】")
    print(f"flash.CHANGE = {flash.CHANGE} (期望: 18)")
    print(f"motion.RESIDUAL = {motion.RESIDUAL} (期望: 35)")
    print(f"motion.MIN_SCORE = {motion.MIN_SCORE} (期望: 50)")

    # 2. 检查全局变量
    print("\n【全局变量】")
    change, max_run = flash.get_flash_params()
    print(f"flash._CURRENT_CHANGE = {change} (期望: 18)")
    print(f"flash._CURRENT_MAX_RUN = {max_run} (期望: 3)")

    residual, min_score = motion.get_motion_params()
    print(f"motion._CURRENT_RESIDUAL = {residual} (期望: 35)")
    print(f"motion._CURRENT_MIN_SCORE = {min_score} (期望: 50)")

    # 3. 检查默认参数
    from app.core.flash_tuning import DEFAULT_PARAMS

    print("\n【默认参数】")
    print(f"DEFAULT_PARAMS.change_threshold = {DEFAULT_PARAMS.change_threshold} (期望: 18)")
    print(f"DEFAULT_PARAMS.residual_threshold = {DEFAULT_PARAMS.residual_threshold} (期望: 35)")
    print(f"DEFAULT_PARAMS.min_score = {DEFAULT_PARAMS.min_score} (期望: 50)")

    # 4. 验证结果
    print("\n" + "="*60)
    print("验证结果")
    print("="*60)

    all_ok = True

    if flash.CHANGE != 18:
        print(f"❌ flash.CHANGE 不正确: {flash.CHANGE} (应该是 18)")
        all_ok = False
    else:
        print(f"✅ flash.CHANGE = 18")

    if motion.RESIDUAL != 35:
        print(f"❌ motion.RESIDUAL 不正确: {motion.RESIDUAL} (应该是 35)")
        all_ok = False
    else:
        print(f"✅ motion.RESIDUAL = 35")

    if motion.MIN_SCORE != 50:
        print(f"❌ motion.MIN_SCORE 不正确: {motion.MIN_SCORE} (应该是 50)")
        all_ok = False
    else:
        print(f"✅ motion.MIN_SCORE = 50")

    if change != 18:
        print(f"❌ 全局变量 _CURRENT_CHANGE 不正确: {change} (应该是 18)")
        all_ok = False
    else:
        print(f"✅ 全局变量 _CURRENT_CHANGE = 18")

    if residual != 35:
        print(f"❌ 全局变量 _CURRENT_RESIDUAL 不正确: {residual} (应该是 35)")
        all_ok = False
    else:
        print(f"✅ 全局变量 _CURRENT_RESIDUAL = 35")

    print("\n" + "="*60)
    if all_ok:
        print("✅ 所有参数正确！")
        print("\n请重启软件后重新测试。")
    else:
        print("❌ 部分参数不正确，请检查代码修改。")

    print("="*60)

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(verify_params())
