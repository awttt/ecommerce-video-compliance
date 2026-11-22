@echo off
REM Windows 自动化打包脚本
REM 确保 OpenCV 和 NumPy 被正确内置到安装包

setlocal enabledelayedexpansion

echo ========================================
echo 御镜视频素材审核系统 - Windows 打包
echo ========================================
echo.

REM 检查是否在项目根目录
if not exist "packaging\evc.spec" (
    echo 错误: 请在项目根目录运行此脚本
    exit /b 1
)

REM 步骤 1: 安装依赖
echo [1/5] 检查并安装 Python 依赖...
pip install -r requirements.txt
if errorlevel 1 (
    echo 错误: 依赖安装失败
    exit /b 1
)

pip install pyinstaller
if errorlevel 1 (
    echo 错误: PyInstaller 安装失败
    exit /b 1
)

REM 验证关键依赖
echo.
echo 验证关键依赖...
python -c "import cv2; print('✓ OpenCV', cv2.__version__)"
if errorlevel 1 (
    echo 错误: OpenCV 未正确安装
    exit /b 1
)

python -c "import numpy; print('✓ NumPy', numpy.__version__)"
if errorlevel 1 (
    echo 错误: NumPy 未正确安装
    exit /b 1
)

python -c "from app.core.motion import available; print('✓ 运动扫描:', '可用' if available() else '不可用')"

REM 步骤 2: 构建前端
echo.
echo [2/5] 构建前端资源...
cd frontend
if not exist "node_modules" (
    echo 首次构建，安装前端依赖...
    call npm install
    if errorlevel 1 (
        echo 错误: 前端依赖安装失败
        cd ..
        exit /b 1
    )
)

call npm run build
if errorlevel 1 (
    echo 错误: 前端构建失败
    cd ..
    exit /b 1
)
cd ..

REM 步骤 3: 清理旧的打包文件
echo.
echo [3/5] 清理旧的打包文件...
if exist "build" (
    rmdir /s /q build
)
if exist "dist\VideoCompliance" (
    rmdir /s /q dist\VideoCompliance
)

REM 步骤 4: 执行 PyInstaller 打包
echo.
echo [4/5] 执行 PyInstaller 打包...
echo 这可能需要几分钟，请耐心等待...
pyinstaller packaging\evc.spec --clean
if errorlevel 1 (
    echo 错误: PyInstaller 打包失败
    exit /b 1
)

REM 步骤 5: 验证打包结果
echo.
echo [5/5] 验证打包结果...

if not exist "dist\VideoCompliance\VideoCompliance.exe" (
    echo 错误: VideoCompliance.exe 未生成
    exit /b 1
)

REM 复制验证脚本
copy packaging\verify_dependencies.py dist\VideoCompliance\ >nul

REM 检查 OpenCV 和 NumPy 是否被打包
echo.
echo 检查依赖文件...

set CV2_FOUND=0
set NUMPY_FOUND=0

if exist "dist\VideoCompliance\cv2" (
    echo ✓ cv2 目录存在
    set CV2_FOUND=1
) else (
    echo ✗ 警告: cv2 目录不存在
)

if exist "dist\VideoCompliance\numpy" (
    echo ✓ numpy 目录存在
    set NUMPY_FOUND=1
) else (
    echo ✗ 警告: numpy 目录不存在
)

REM 运行依赖验证
echo.
echo 运行完整依赖验证...
cd dist\VideoCompliance
VideoCompliance.exe verify_dependencies.py
set VERIFY_RESULT=!errorlevel!
cd ..\..

if !VERIFY_RESULT! neq 0 (
    echo.
    echo ========================================
    echo 警告: 依赖验证失败
    echo 请检查打包配置
    echo ========================================
    exit /b 1
)

REM 步骤 6: 输出统计信息
echo.
echo ========================================
echo 打包成功！
echo ========================================
echo.

REM 计算目录大小
for /f "tokens=3" %%a in ('dir /s /-c "dist\VideoCompliance" ^| findstr /C:"个文件"') do set SIZE=%%a
echo 打包目录: dist\VideoCompliance
echo 文件大小: %SIZE% 字节
echo.

REM 列出主要文件
echo 主要文件:
echo   ✓ VideoCompliance.exe
if exist "dist\VideoCompliance\cv2" echo   ✓ cv2 (OpenCV)
if exist "dist\VideoCompliance\numpy" echo   ✓ numpy
if exist "dist\VideoCompliance\frontend\dist" echo   ✓ frontend/dist
echo.

REM 下一步提示
echo ========================================
echo 下一步:
echo ========================================
echo 1. 测试运行:
echo    cd dist\VideoCompliance
echo    VideoCompliance.exe
echo.
echo 2. 制作安装包（需要 Inno Setup）:
echo    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss
echo.
echo 3. 在干净的 Windows 虚拟机上测试安装包
echo ========================================

endlocal
