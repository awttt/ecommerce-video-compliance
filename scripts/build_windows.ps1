# 在 Windows 上构建桌面程序。
# 用法：powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "需要先安装 Python 3.11 或更高版本，并加入 PATH。"
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "需要先安装 Node.js 18 或更高版本。"
}

if (-not (Test-Path .venv)) {
    python -m venv .venv
}

$Python = Join-Path $Root ".venv\Scripts\python.exe"
& $Python -m pip install --upgrade pip
& $Python -m pip install -r requirements.txt pyinstaller

Push-Location frontend
npm install
npm run build
Pop-Location

& $Python -m PyInstaller --noconfirm --clean packaging\evc.spec

Write-Host ""
Write-Host "打包完成：dist\VideoCompliance\VideoCompliance.exe"
Write-Host "请安装 FFmpeg，或把 ffmpeg.exe 和 ffprobe.exe 放到 exe 同一目录。"
Write-Host "Windows 10/11 需要系统自带的 WebView2 运行库。"
