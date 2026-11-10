# 开发启动。界面热更新时另开终端：cd frontend; npm run dev
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
if (-not (Test-Path .venv)) { python -m venv .venv }
$Python = Join-Path $Root ".venv\Scripts\python.exe"
& $Python -m pip install -r requirements.txt
Push-Location frontend
npm install
npm run build
Pop-Location
& $Python run.py
