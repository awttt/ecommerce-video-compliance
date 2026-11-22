# 闪帧检测稳定性改进方案

## 问题背景

部分用户反馈闪帧检测不稳定，有的电脑可以检测到，有的电脑检测不到。

## 问题原因分析

### 1. 依赖缺失
运动扫描依赖 `opencv-python` 和 `numpy`。如果用户环境缺少这些库，只会使用亮度比较，检测准确性降低。

### 2. 固定阈值不适配
代码使用硬编码的检测阈值：
- 亮度变化阈值：24
- 运动残差阈值：45
- 最小分数：60

不同视频的质量、压缩程度、噪声水平差异很大，固定阈值无法适配所有情况。

### 3. 缺少诊断信息
用户无法得知检测失败的原因，难以调整和优化。

## 改进方案

### 1. 自适应参数调整（flash_tuning.py）

根据视频特征自动调整检测参数：

```python
from app.core.flash_tuning import adaptive_params

params = adaptive_params(
    width=scan_w,
    height=scan_h,
    noise_level=10.0,  # 可选：估算的噪声水平
    is_compressed=True,  # 可选：是否高度压缩
)
```

**调整策略**：
- 低分辨率视频：降低阈值 30%
- 高噪声视频（>20）：提高阈值，避免误检
- 低噪声视频（<5）：降低阈值 20%，提升灵敏度
- 高压缩视频：提高阈值 30%，放宽连续帧限制

### 2. 增强的诊断工具（flash_diagnostics.py）

#### CLI 诊断命令

```bash
# 诊断运行环境
python -m app.cli diagnose

# 诊断视频文件
python -m app.cli diagnose video.mp4
```

**输出内容**：
- OpenCV 和 NumPy 安装状态
- 运动扫描可用性
- 视频分辨率、帧率、编码
- 扫描尺寸
- 潜在问题和改进建议

#### 详细日志

在扫描完成后自动输出诊断摘要：
- 检测到的闪帧数量
- 检测密度（个/分钟）
- 质量评估和建议

### 3. 可配置参数

修改后的代码支持动态调整参数：

```python
from app.core.flash import set_flash_params
from app.core.motion import set_motion_params

# 自定义亮度检测参数
set_flash_params(change_threshold=20, max_run=4)

# 自定义运动检测参数
set_motion_params(residual_threshold=40, min_score=50)
```

### 4. 自动集成

`scan_flashes()` 函数现在自动启用自适应参数：

```python
spans = await scan_flashes(
    video,
    width=width,
    height=height,
    fps=fps,
    duration=duration,
    sample_fps=sample_fps,
    cancel=cancel,
    motion_enabled=True,
    adaptive_params=True,  # 自动调整参数
)
```

## 使用指南

### 1. 首次使用 - 环境诊断

```bash
# 检查环境
python -m app.cli diagnose

# 如果提示缺少依赖，安装：
pip install opencv-python numpy
```

### 2. 检测效果不佳 - 视频诊断

```bash
# 诊断特定视频
python -m app.cli diagnose problem_video.mp4
```

根据诊断报告的建议调整：
- 低分辨率：使用更高分辨率的源视频
- 高帧率：考虑降低扫描帧率
- 特殊编码：转换为 H.264

### 3. 对比测试

```bash
# 对比自适应参数和默认参数的效果
python test_flash_detection.py video.mp4 --compare
```

### 4. 调整参数（高级）

如果自适应参数仍不理想，可以手动调整：

```python
# 在调用 scan_flashes 前设置
from app.core.flash import set_flash_params
from app.core.motion import set_motion_params

# 降低阈值，提高灵敏度（可能增加误检）
set_flash_params(change_threshold=18, max_run=3)
set_motion_params(residual_threshold=35, min_score=45)

# 或提高阈值，减少误检（可能遗漏部分闪帧）
set_flash_params(change_threshold=30, max_run=3)
set_motion_params(residual_threshold=55, min_score=75)
```

## 参数说明

### 亮度检测参数

| 参数 | 默认值 | 说明 | 调整建议 |
|------|--------|------|----------|
| change_threshold | 24 | 小块亮度至少变化多少才算闪帧 | 降低=更敏感，提高=更严格 |
| max_run | 3 | 闪帧最多连续几帧 | 压缩视频可适当增加 |

### 运动检测参数

| 参数 | 默认值 | 说明 | 调整建议 |
|------|--------|------|----------|
| residual_threshold | 45 | 对齐后至少还要差多少灰阶 | 降低=更敏感，提高=更严格 |
| min_score | 60 | 笔画密度最低分数 | 降低=检测更多，提高=只检测明显的 |

## 预期效果

- ✅ 不同质量视频的检测稳定性提升
- ✅ 用户可以快速诊断问题原因
- ✅ 支持根据实际情况微调参数
- ✅ 自动记录详细的检测日志

## 后续优化方向

1. **噪声估算**：从视频采样帧自动估算噪声水平
2. **机器学习调参**：根据历史检测结果自动优化参数
3. **多阶段检测**：先用高阈值快速过滤，再用低阈值精细检测
4. **用户反馈循环**：收集用户标注，持续改进算法

## 兼容性

所有改动向后兼容，现有代码无需修改即可获得自适应参数的好处。

如需关闭自适应参数（使用固定阈值）：

```python
spans = await scan_flashes(
    video,
    # ... 其他参数
    adaptive_params=False,  # 禁用自适应
)
```
