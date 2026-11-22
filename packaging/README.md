# 打包说明

## 概述

本目录包含将应用打包为 Windows 可执行程序的配置文件。

## OpenCV 和 NumPy 内置说明

✅ **OpenCV 和 NumPy 已配置为自动内置到安装包中**

用户安装后**无需**：
- 安装 Python 环境
- 手动安装 opencv-python
- 手动安装 numpy
- 配置任何环境变量

所有依赖都会被打包到 `.exe` 程序中，开箱即用。

## 文件说明

| 文件 | 说明 |
|------|------|
| `evc.spec` | PyInstaller 配置文件（已配置 OpenCV/NumPy） |
| `installer.iss` | Inno Setup 安装包配置 |
| `build_windows.bat` | Windows 自动化打包脚本 |
| `verify_dependencies.py` | 打包后依赖验证脚本 |
| `hooks/hook-cv2.py` | OpenCV 打包 hook |
| `hooks/hook-numpy.py` | NumPy 打包 hook |
| `WINDOWS_BUILD.md` | 详细的 Windows 打包文档 |

## 快速开始（Windows）

### 方式 1: 自动化脚本（推荐）

在项目根目录运行：

```batch
packaging\build_windows.bat
```

脚本会自动：
1. ✓ 安装所有依赖（包括 OpenCV 和 NumPy）
2. ✓ 构建前端
3. ✓ 执行 PyInstaller 打包
4. ✓ 验证 OpenCV 和 NumPy 是否正确内置
5. ✓ 输出打包统计

### 方式 2: 手动打包

```batch
# 1. 安装依赖
pip install -r requirements.txt
pip install pyinstaller

# 2. 构建前端
cd frontend
npm install
npm run build
cd ..

# 3. 打包
pyinstaller packaging\evc.spec

# 4. 验证
cd dist\VideoCompliance
VideoCompliance.exe -c "import cv2, numpy; print('OK')"
```

## 打包输出

打包成功后：

```
dist/
└── VideoCompliance/
    ├── VideoCompliance.exe       # 主程序
    ├── cv2/                      # OpenCV（已内置）
    ├── numpy/                    # NumPy（已内置）
    ├── frontend/dist/            # 前端资源
    └── ... 其他依赖文件
```

## 验证内置依赖

### 方式 1: 运行验证脚本

```batch
cd dist\VideoCompliance
copy ..\..\packaging\verify_dependencies.py .
VideoCompliance.exe verify_dependencies.py
```

预期输出：
```
依赖验证
========================================
...
✓ NumPy 可用
✓ OpenCV 可用
✓ 运动扫描可用
✓ 闪帧检测可用
...
✓ 所有依赖验证通过！
```

### 方式 2: 手动检查

```batch
# 检查目录是否存在
dir dist\VideoCompliance\cv2
dir dist\VideoCompliance\numpy

# 运行测试
dist\VideoCompliance\VideoCompliance.exe -c "from app.core.motion import available; print(available())"
```

应该输出 `True`。

## 常见问题

### Q: 打包后运行提示 "No module named 'cv2'"

**A**: OpenCV 未被正确打包。检查：

1. `requirements.txt` 中是否有 `opencv-python-headless>=4.9`
2. `evc.spec` 中 `hookspath` 是否指向 `packaging/hooks`
3. 是否存在 `packaging/hooks/hook-cv2.py`

手动修复：
```batch
pip uninstall opencv-python opencv-python-headless
pip install opencv-python-headless>=4.9
```

重新打包。

### Q: 打包后运动扫描不可用

**A**: 检查 OpenCV 版本和功能：

```python
import cv2
print(cv2.__version__)  # 应该 >= 4.9
flow = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_FAST)  # 应该成功
```

### Q: 打包文件太大

**A**: 当前配置已经优化：

- 使用 `opencv-python-headless` 而非 `opencv-python`（节省 ~100MB）
- 排除不需要的模块
- 典型大小：150-250 MB（包含所有依赖）

进一步优化（可选）：
```python
# 在 evc.spec 中添加
excludes=['matplotlib', 'scipy', 'pandas', 'IPython']
```

### Q: 在虚拟机测试时运动扫描不可用

**A**: 确保虚拟机安装了 Visual C++ Redistributable：

下载：https://aka.ms/vs/17/release/vc_redist.x64.exe

或在打包时包含：
```python
# 在 evc.spec 的 binaries 中添加
binaries=[('C:/Windows/System32/vcruntime140.dll', '.')]
```

## 制作安装包

使用 Inno Setup 制作 `.exe` 安装程序：

```batch
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss
```

输出：`output/VideoCompliance-Setup.exe`

安装包会包含 `dist/VideoCompliance` 中的所有文件，包括 OpenCV 和 NumPy。

## 测试清单

在发布前，确保在干净的 Windows 虚拟机上测试：

- [ ] 运行安装包
- [ ] 启动程序
- [ ] 检查日志：运动扫描应该显示"可用"
- [ ] 执行闪帧检测任务
- [ ] 验证检测结果

测试环境要求：
- Windows 10/11 x64
- **无需** Python 环境
- **无需** 任何手动安装的依赖

## 技术细节

### OpenCV 打包机制

1. **依赖声明**: `requirements.txt` 中使用 `opencv-python-headless`
2. **模块收集**: `evc.spec` 自动收集所有 `cv2` 子模块
3. **动态库**: 自定义 hook 收集 `.pyd` 和 `.dll` 文件
4. **隐式导入**: 显式声明核心模块避免遗漏

### NumPy 打包机制

1. **核心模块**: 显式包含 `numpy.core._multiarray_umath` 等
2. **C 扩展**: 自动收集所有 `.pyd` 文件
3. **子模块**: 使用 `collect_submodules('numpy')` 全量收集

### PyInstaller Hooks

自定义 hooks 位于 `packaging/hooks/`：

- `hook-cv2.py`: 确保 OpenCV 所有组件被包含
- `hook-numpy.py`: 确保 NumPy C 扩展被包含

这些 hooks 在 `evc.spec` 中通过 `hookspath` 参数加载。

## 相关文档

- [详细 Windows 打包文档](WINDOWS_BUILD.md)
- [PyInstaller 官方文档](https://pyinstaller.org/)
- [Inno Setup 官方文档](https://jrsoftware.org/isinfo.php)

## 总结

✅ OpenCV 和 NumPy **已完全内置**到打包配置中

✅ 用户安装后**无需任何额外操作**

✅ 运动扫描和闪帧检测**开箱即用**

如有打包问题，请参考 [WINDOWS_BUILD.md](WINDOWS_BUILD.md) 或提交 Issue。
