# Windows 打包说明

## 确保 OpenCV 和 NumPy 内置到安装包

### 1. 依赖已在 requirements.txt 中声明

```txt
numpy>=1.26
opencv-python-headless>=4.9
```

**注意**: 使用 `opencv-python-headless` 而不是 `opencv-python`，因为：
- 更小的体积（不含 GUI 相关的依赖）
- 适合服务端/桌面打包
- 包含所有图像处理功能

### 2. PyInstaller 配置已增强

`evc.spec` 文件已经配置为：

1. **自动收集依赖**：
   - 使用 `collect_submodules()` 收集所有子模块
   - 使用 `collect_data_files()` 收集数据文件
   - 使用 `collect_dynamic_libs()` 收集动态库

2. **显式声明隐藏导入**：
   ```python
   hiddenimports = [
       "cv2",
       "numpy",
       "numpy.core",
       "numpy.core._multiarray_umath",
       # ... 更多核心模块
   ]
   ```

3. **自定义 Hooks**：
   - `packaging/hooks/hook-cv2.py` - OpenCV 专用 hook
   - `packaging/hooks/hook-numpy.py` - NumPy 专用 hook

### 3. Windows 打包步骤

#### 准备环境

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 安装打包工具
pip install pyinstaller

# 3. 确认依赖已安装
python -c "import cv2; print('OpenCV:', cv2.__version__)"
python -c "import numpy; print('NumPy:', numpy.__version__)"
```

#### 构建前端

```bash
cd frontend
npm install
npm run build
cd ..
```

#### 执行打包

```bash
# 在项目根目录执行
pyinstaller packaging/evc.spec
```

打包完成后会生成：
- `dist/VideoCompliance/` - 包含所有文件的目录
- `dist/VideoCompliance/VideoCompliance.exe` - 主程序

#### 验证依赖

```bash
# 进入打包目录
cd dist/VideoCompliance

# 运行验证脚本
VideoCompliance.exe -c "from packaging.verify_dependencies import main; main()"

# 或者手动测试
VideoCompliance.exe -c "import cv2; import numpy; print('OK')"
```

更完整的验证：

```bash
# 将验证脚本复制到打包目录
copy ..\..\packaging\verify_dependencies.py .

# 运行完整验证
VideoCompliance.exe verify_dependencies.py
```

### 4. 常见问题排查

#### 问题 1: 打包后运行提示 "No module named 'cv2'"

**原因**: OpenCV 的动态库未被正确包含

**解决**:
1. 检查 `packaging/hooks/hook-cv2.py` 是否存在
2. 确认 `evc.spec` 中 `hookspath` 指向 `packaging/hooks`
3. 手动检查 `dist/VideoCompliance` 目录中是否有 `cv2` 相关的 `.pyd` 文件

**手动添加**（如果自动收集失败）:
```python
# 在 evc.spec 的 Analysis 部分之后添加
import cv2
cv2_path = os.path.dirname(cv2.__file__)
a.binaries += [(f'cv2/{f}', os.path.join(cv2_path, f), 'BINARY') 
               for f in os.listdir(cv2_path) if f.endswith('.pyd') or f.endswith('.dll')]
```

#### 问题 2: 打包后运行提示 NumPy 相关错误

**原因**: NumPy 的 C 扩展模块未被正确包含

**解决**:
1. 确认 `hiddenimports` 中包含：
   ```python
   "numpy.core._multiarray_umath",
   "numpy.core._dtype_ctypes",
   ```
2. 检查 `dist/VideoCompliance/numpy/core` 目录是否存在

#### 问题 3: 运动扫描不可用

**原因**: OpenCV 的光流算法依赖缺失

**检查**:
```python
from app.core.motion import available
print(available())  # 应该输出 True
```

**解决**:
1. 确认使用的是 `opencv-python-headless>=4.9`
2. 检查 `cv2.DISOpticalFlow` 是否可用：
   ```python
   import cv2
   flow = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_FAST)
   ```

#### 问题 4: 打包文件过大

**优化措施**:

1. 使用 `opencv-python-headless` 而不是 `opencv-python`（节省约 100MB）

2. 排除不需要的模块（在 `evc.spec` 中）：
   ```python
   excludes=[
       'matplotlib',
       'scipy',
       'pandas',
       'IPython',
       'jupyter',
   ]
   ```

3. 使用 UPX 压缩（可选，但可能导致杀毒软件误报）：
   ```python
   exe = EXE(
       ...,
       upx=True,
       upx_exclude=['vcruntime140.dll', 'python*.dll'],
   )
   ```

### 5. 打包检查清单

打包完成后，检查以下内容：

- [ ] `dist/VideoCompliance/VideoCompliance.exe` 存在
- [ ] `dist/VideoCompliance/cv2/` 目录存在且包含 `.pyd` 文件
- [ ] `dist/VideoCompliance/numpy/` 目录存在且包含 `.pyd` 文件
- [ ] 运行 `verify_dependencies.py` 全部通过
- [ ] 运行程序并测试闪帧检测功能
- [ ] 在干净的 Windows 虚拟机上测试（无 Python 环境）

### 6. Inno Setup 安装包

如果需要制作安装程序，使用 `packaging/installer.iss`：

```bash
# 使用 Inno Setup 编译器
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss
```

安装包会包含 `dist/VideoCompliance` 目录中的所有文件，包括 OpenCV 和 NumPy。

### 7. 测试环境要求

**最小测试环境**:
- Windows 10/11 x64
- **无需** Python 环境
- **无需** 手动安装 OpenCV 或 NumPy
- Visual C++ Redistributable 2015-2022（Windows 通常已预装）

**验证方法**:
在干净的 Windows 虚拟机上：
1. 安装打包好的程序
2. 运行程序
3. 执行闪帧检测任务
4. 检查日志中 "运动扫描: 可用"

### 8. 自动化打包脚本

创建 `build_windows.bat`：

```batch
@echo off
echo 开始构建 Windows 安装包
echo.

echo [1/4] 安装依赖
pip install -r requirements.txt
pip install pyinstaller
if errorlevel 1 goto error

echo.
echo [2/4] 构建前端
cd frontend
call npm install
call npm run build
cd ..
if errorlevel 1 goto error

echo.
echo [3/4] PyInstaller 打包
pyinstaller packaging/evc.spec
if errorlevel 1 goto error

echo.
echo [4/4] 验证依赖
copy packaging\verify_dependencies.py dist\VideoCompliance\
cd dist\VideoCompliance
VideoCompliance.exe verify_dependencies.py
if errorlevel 1 goto error

echo.
echo ========================================
echo 打包成功！
echo 输出目录: dist\VideoCompliance
echo ========================================
goto end

:error
echo.
echo ========================================
echo 打包失败！
echo ========================================
exit /b 1

:end
```

### 9. 依赖版本说明

| 依赖 | 版本要求 | 说明 |
|------|---------|------|
| numpy | >= 1.26 | 支持 Python 3.11+ |
| opencv-python-headless | >= 4.9 | 包含光流算法 |
| PyInstaller | >= 6.0 | 支持 Python 3.11+ |

### 10. 相关文档

- PyInstaller 官方文档: https://pyinstaller.org/
- OpenCV 打包参考: https://github.com/pyinstaller/pyinstaller/wiki/Recipe-OpenCV
- NumPy 打包参考: https://github.com/pyinstaller/pyinstaller/wiki/Recipe-Numpy

## 总结

通过以上配置，OpenCV 和 NumPy 会被自动内置到 Windows 安装包中，用户**无需手动安装任何 Python 依赖**，直接运行安装包即可使用完整的闪帧检测功能。
