# 闪帧检测参数调整 - 测试指南

## 问题

1-2秒的 "9.9元" 没有被检测到，即使已经降低了阈值。

## 已完成的修改

✅ 降低阈值：
- CHANGE: 24 → 18
- RESIDUAL: 45 → 35
- MIN_SCORE: 60 → 50

✅ 清除 Python 缓存

✅ 参数验证通过

## 测试步骤

### 步骤1: 重启软件

**重要**: 必须完全关闭并重启软件，确保新参数生效。

```bash
# 如果是开发模式
pkill -f "python.*run.py"  # 杀掉旧进程
python run.py               # 重新启动

# 如果是打包后的应用
# 完全退出应用，然后重新打开
```

### 步骤2: 验证参数

在重启后，运行验证脚本：

```bash
python3 verify_params.py
```

应该看到所有 ✅。

### 步骤3: 重新测试视频

1. 打开软件
2. 拖入 `1928ecca04ef28e91fe7af5b2a0d3cc7.mp4`
3. 等待检测完成
4. 查看日志中的闪帧数量

**预期**：
- 闪帧数量应该 **> 38**（例如 45-50 个）
- 如果还是 38，说明参数仍未生效

### 步骤4: 查看检测结果

```bash
# 查看最新任务ID
sqlite3 data/app.db "
SELECT id, created_at 
FROM jobs 
WHERE filename='1928ecca04ef28e91fe7af5b2a0d3cc7.mp4' 
ORDER BY created_at DESC 
LIMIT 1;
"

# 查看 1-2秒 的检测结果（替换 <job_id>）
sqlite3 data/app.db "
SELECT start_sec, end_sec, category, text 
FROM findings 
WHERE job_id='<job_id>' 
AND start_sec BETWEEN 1.0 AND 2.0;
"
```

**预期**：应该看到 1-2秒 之间有记录了。

### 步骤5: 查看日志

```bash
tail -50 data/logs/app.log | grep "闪帧\|检测"
```

**预期输出类似**：
```
闪帧扫描 X 秒，亮度 45 帧，运动 12 帧，送去识别 50 帧
检测密度: 200+ 个/分钟
```

注意数字的变化。

## 如果仍然是 38 帧

如果重启后仍然是 38 帧，可能原因：

### 原因1: 参数确实没生效

检查：
```python
# 在软件启动后，在某个 API 端点加入调试代码
from app.core.flash import get_flash_params
from app.core.motion import get_motion_params

print("Flash params:", get_flash_params())    # 应该是 (18, 3)
print("Motion params:", get_motion_params())  # 应该是 (35, 50)
```

### 原因2: 38 是某个硬编码限制

搜索代码中是否有 38 这个数字：

```bash
grep -r "38" app/core/*.py | grep -v "2038\|1938\|0.38"
```

### 原因3: 需要更激进的阈值

如果确认参数生效了但还是检测不到，尝试更激进的值：

```python
# 临时修改（在 app/core/flash.py）
CHANGE = 15  # 从 18 进一步降到 15

# 在 app/core/motion.py
RESIDUAL = 30  # 从 35 降到 30
MIN_SCORE = 40  # 从 50 降到 40
```

然后再次：
1. 清除缓存
2. 重启软件
3. 重新测试

## 终极解决方案

如果以上都不行，可以尝试：

### 方案A: 提高抽帧密度

在任务配置中，把抽帧 FPS 从 1.0 改为 2.0：

优点：
- 覆盖更全，不依赖闪帧检测
- 简单可靠

缺点：
- 处理时间翻倍
- OCR 请求翻倍

### 方案B: 调试具体帧

手动提取 1-2秒 的帧，查看特征：

```bash
ffmpeg -ss 1.0 -i ~/Downloads/1928ecca04ef28e91fe7af5b2a0d3cc7.mp4 \
  -t 1.0 -vf "fps=30" -q:v 2 frame_%03d.jpg
```

然后用 Python 分析这些帧的亮度变化：

```python
from PIL import Image
import numpy as np

frames = []
for i in range(1, 31):
    img = Image.open(f'frame_{i:03d}.jpg').convert('L')  # 灰度
    arr = np.array(img)
    mean = arr.mean()
    frames.append(mean)

# 打印亮度变化
for i in range(1, len(frames)):
    diff = abs(frames[i] - frames[i-1])
    print(f"帧{i}: 亮度={frames[i]:.1f}, 变化={diff:.1f}")
```

找出变化最大的帧，看看是否 > 18。

### 方案C: 禁用自适应参数

在 `app/core/ffmpeg.py` 的 `scan_flashes()` 调用处：

```python
spans = await scan_flashes(
    video,
    ...,
    adaptive_params=False,  # 禁用自适应
)
```

然后手动在代码开头设置参数：

```python
# 在 scan_flashes() 开头
from app.core.flash import set_flash_params
from app.core.motion import set_motion_params

set_flash_params(change_threshold=15, max_run=4)
set_motion_params(residual_threshold=30, min_score=40)
```

## 调试日志

在 `app/core/flash.py` 的 `_block_flashed()` 函数中添加日志：

```python
def _block_flashed(before: int, middle: int, after: int) -> bool:
    change_threshold = _CURRENT_CHANGE
    jumped = abs(middle - before)
    returned = abs(middle - after)
    stayed = abs(after - before)
    
    # 添加调试日志
    if jumped >= change_threshold * 0.8 or returned >= change_threshold * 0.8:
        print(f"接近阈值: jumped={jumped}, returned={returned}, "
              f"stayed={stayed}, threshold={change_threshold}")
    
    if jumped < change_threshold or returned < change_threshold:
        return False
    return stayed * 2 <= min(jumped, returned)
```

重新运行，看看是否有"接近阈值"的日志。

## 总结

**关键检查点**：

1. ✅ 代码已修改（verify_params.py 通过）
2. ⚠️ **软件必须重启**（这是最常见的问题）
3. ⚠️ 验证闪帧数量是否变化（38 → 更多）
4. ⚠️ 查看 1-2秒 是否有检测结果

**下一步**：

1. **立即重启软件**
2. 重新测试视频
3. 检查日志中闪帧数量
4. 如果还是 38，尝试更激进的阈值（CHANGE=15, RESIDUAL=30）
