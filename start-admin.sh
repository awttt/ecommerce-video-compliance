#!/usr/bin/env bash
# 启动本机桌面套件：窗口、审核工具 http://127.0.0.1:8765 、管理控制台 http://127.0.0.1:8780 。
# 优先用本仓库的 .venv。没有时再试旁边主仓库的虚拟环境。
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"

export PATH="/usr/local/bin:${PATH}"
export PYTHONUNBUFFERED=1

PYTHON=""
if [[ -x "${ROOT}/.venv/bin/python" ]]; then
  PYTHON="${ROOT}/.venv/bin/python"
elif [[ -x "${ROOT}/../ecommerce-video-compliance/.venv/bin/python" ]]; then
  PYTHON="${ROOT}/../ecommerce-video-compliance/.venv/bin/python"
fi

if [[ -z "$PYTHON" ]]; then
  echo "未找到虚拟环境。请在本仓库创建：" >&2
  echo "  python3.12 -m venv .venv && .venv/bin/python -m pip install -r requirements-dev.txt" >&2
  exit 1
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "虚拟环境里的 Python 低于 3.11。" >&2
  exit 1
fi

if [[ ! -f "${ROOT}/frontend/dist/index.html" ]]; then
  echo "未找到审核界面。请先执行：cd frontend && npm run build" >&2
  exit 1
fi

if [[ ! -f "${ROOT}/admin/web/dist/index.html" ]]; then
  echo "未找到管理控制台界面。请先执行：cd admin/web && npm run build" >&2
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  echo "未找到 ffmpeg 或 ffprobe。请安装 FFmpeg 并加入 PATH，或在 .env 里设置 EVC_FFMPEG / EVC_FFPROBE。" >&2
  exit 1
fi

if lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1 || lsof -nP -iTCP:8780 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "端口 8765 或 8780 已被占用。请先停掉旧的桌面套件再启动。" >&2
  lsof -nP -iTCP:8765 -sTCP:LISTEN 2>/dev/null || true
  lsof -nP -iTCP:8780 -sTCP:LISTEN 2>/dev/null || true
  exit 1
fi

echo "Python $("$PYTHON" -V 2>&1)"
echo "审核工具  http://127.0.0.1:8765"
echo "管理控制台  http://127.0.0.1:8780"

exec "$PYTHON" -u "${ROOT}/run.py"
