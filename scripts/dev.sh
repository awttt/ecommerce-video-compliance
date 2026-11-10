#!/usr/bin/env bash
# 开发：先构建界面，再启动本地服务。热更新界面时另开终端执行 npm run dev。
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m pip install -r requirements-dev.txt
(cd frontend && npm install && npm run build)
exec python3 run.py --no-window --browser
