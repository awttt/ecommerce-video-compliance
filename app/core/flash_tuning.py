"""自适应闪帧检测参数调优。

根据视频特征自动调整检测阈值，提升不同质量视频的检测稳定性。
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FlashParams:
    """闪帧检测参数集。"""
    change_threshold: int  # 亮度变化阈值
    residual_threshold: int  # 运动扫描残差阈值
    min_score: int  # 最低分数
    max_run: int  # 最多连续帧数


DEFAULT_PARAMS = FlashParams(
    change_threshold=12,  # 极低对比度检测
    residual_threshold=28,
    min_score=35,
    max_run=4,
)


def estimate_video_noise(frames_sample: list[bytes], width: int, height: int) -> float:
    """从采样帧估算视频噪声水平。

    计算相邻帧间的平均像素差异，作为噪声+运动的混合指标。
    """
    if len(frames_sample) < 2 or width < 2 or height < 2:
        return 0.0

    diffs: list[float] = []
    for i in range(len(frames_sample) - 1):
        frame1 = memoryview(frames_sample[i])
        frame2 = memoryview(frames_sample[i + 1])
        if len(frame1) < width * height or len(frame2) < width * height:
            continue

        # 采样对比，不用全部像素
        sample_step = max(1, (width * height) // 10000)
        total_diff = 0
        count = 0
        for j in range(0, min(len(frame1), len(frame2)), sample_step):
            total_diff += abs(frame1[j] - frame2[j])
            count += 1

        if count > 0:
            diffs.append(total_diff / count)

    return statistics.median(diffs) if diffs else 0.0


def adaptive_params(
    width: int,
    height: int,
    noise_level: float = 0.0,
    is_compressed: bool = False,
) -> FlashParams:
    """根据视频特征调整检测参数。

    Args:
        width: 扫描宽度
        height: 扫描高度
        noise_level: 噪声水平（0-255，通常 0-30）
        is_compressed: 是否高度压缩

    Returns:
        调整后的参数
    """
    params = DEFAULT_PARAMS

    # 1. 根据分辨率调整
    pixels = width * height
    if pixels < 320 * 180:  # 低分辨率视频
        log.info("检测到低分辨率视频，降低阈值")
        params = FlashParams(
            change_threshold=int(params.change_threshold * 0.7),
            residual_threshold=int(params.residual_threshold * 0.7),
            min_score=int(params.min_score * 0.6),
            max_run=params.max_run,
        )

    # 2. 根据噪声水平调整
    if noise_level > 0:
        if noise_level > 20:  # 高噪声
            log.info("检测到高噪声视频（%.1f），提高阈值", noise_level)
            noise_factor = min(2.0, 1.0 + (noise_level - 20) / 40)
            params = FlashParams(
                change_threshold=int(params.change_threshold * noise_factor),
                residual_threshold=int(params.residual_threshold * noise_factor),
                min_score=params.min_score,
                max_run=params.max_run,
            )
        elif noise_level < 5:  # 低噪声，可以更敏感
            log.info("检测到低噪声视频（%.1f），降低阈值", noise_level)
            params = FlashParams(
                change_threshold=int(params.change_threshold * 0.8),
                residual_threshold=int(params.residual_threshold * 0.8),
                min_score=int(params.min_score * 0.8),
                max_run=params.max_run,
            )

    # 3. 高压缩视频
    if is_compressed:
        log.info("检测到高压缩视频，提高阈值并放宽连续帧限制")
        params = FlashParams(
            change_threshold=int(params.change_threshold * 1.3),
            residual_threshold=int(params.residual_threshold * 1.3),
            min_score=params.min_score,
            max_run=min(5, params.max_run + 1),  # 压缩可能让闪帧拖长
        )

    log.info(
        "自适应参数：change=%d, residual=%d, min_score=%d, max_run=%d",
        params.change_threshold,
        params.residual_threshold,
        params.min_score,
        params.max_run,
    )

    return params


def validate_detection_quality(
    detected_count: int,
    video_duration: float,
    expected_density: float = 2.0,
) -> dict[str, object]:
    """验证检测结果的合理性。

    Args:
        detected_count: 检测到的闪帧数量
        video_duration: 视频时长（秒）
        expected_density: 预期密度（个/分钟）

    Returns:
        包含 is_reasonable, reason 的字典
    """
    if video_duration <= 0:
        return {"is_reasonable": True, "reason": ""}

    density = (detected_count / video_duration) * 60  # 转换为每分钟

    # 合理范围：0.1 到 20 个/分钟
    if density > 20:
        return {
            "is_reasonable": False,
            "reason": f"检测过于敏感，密度 {density:.1f} 个/分钟，建议提高阈值",
        }

    if density < 0.05 and video_duration > 10:
        return {
            "is_reasonable": False,
            "reason": f"检测可能遗漏，密度 {density:.2f} 个/分钟，建议降低阈值",
        }

    return {"is_reasonable": True, "reason": ""}
