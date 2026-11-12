#!/usr/bin/env bash
# 本地开发启动：后端 http://127.0.0.1:8765 ，界面热更新 http://127.0.0.1:5173 。
# Ctrl+C 会同时停掉两边。依赖未安装时请先看 README 的「开发运行」，不要用本脚本装依赖。
set -eu

cd "$(dirname "$0")"
ROOT="$(pwd)"
API_PORT=8765
VITE_PORT=5173
API_URL="http://127.0.0.1:${API_PORT}"
VITE_URL="http://127.0.0.1:${VITE_PORT}"

export PATH="/usr/local/bin:${PATH}"
export PYTHONUNBUFFERED=1

api_pid=""
vite_pid=""

node_ok() {
  command -v node >/dev/null 2>&1 || return 1
  node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 18 ? 0 : 1)'
}

load_node() {
  if node_ok; then
    return 0
  fi
  local nvm_dir="${NVM_DIR:-$HOME/.nvm}"
  if [[ ! -s "${nvm_dir}/nvm.sh" ]]; then
    return 1
  fi
  export NVM_DIR="$nvm_dir"
  set +eu
  # shellcheck disable=SC1091
  . "$NVM_DIR/nvm.sh"
  local version
  for version in 22 20 18; do
    if [[ "$(nvm version "$version" 2>/dev/null)" != "N/A" ]]; then
      nvm use "$version" >/dev/null
      break
    fi
  done
  set -eu
  node_ok
}

kill_tree() {
  local pid="$1"
  local child
  local children
  children="$(pgrep -P "$pid" 2>/dev/null || true)"
  for child in $children; do
    kill_tree "$child"
  done
  kill "$pid" 2>/dev/null || true
}

cleanup() {
  local had_api="$api_pid"
  local had_vite="$vite_pid"
  trap - EXIT INT TERM
  if [[ -n "$had_api" ]]; then
    kill_tree "$had_api"
  fi
  if [[ -n "$had_vite" ]]; then
    kill_tree "$had_vite"
  fi
  api_pid=""
  vite_pid=""
  if [[ -z "$had_api" && -z "$had_vite" ]]; then
    return 0
  fi
  sleep 0.4
  if [[ -n "$had_api" ]] && kill -0 "$had_api" 2>/dev/null; then
    kill -9 "$had_api" 2>/dev/null || true
  fi
  if [[ -n "$had_vite" ]] && kill -0 "$had_vite" 2>/dev/null; then
    kill -9 "$had_vite" 2>/dev/null || true
  fi
}

on_stop() {
  cleanup
  exit 130
}

trap cleanup EXIT
trap on_stop INT TERM

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "未找到 .venv。请先创建虚拟环境并安装依赖：" >&2
  echo "  python3.12 -m venv .venv && .venv/bin/python -m pip install -r requirements-dev.txt" >&2
  exit 1
fi

if ! "$ROOT/.venv/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "虚拟环境里的 Python 低于 3.11。" >&2
  exit 1
fi

if ! load_node; then
  echo "需要 Node.js 18 或更高版本。可执行：nvm install 22" >&2
  exit 1
fi

if [[ ! -d "$ROOT/frontend/node_modules" ]]; then
  echo "未找到 frontend/node_modules。请先执行：cd frontend && npm install" >&2
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  echo "未找到 ffmpeg 或 ffprobe。请安装 FFmpeg 并加入 PATH，或在 .env 里设置 EVC_FFMPEG / EVC_FFPROBE。" >&2
  exit 1
fi

if lsof -nP -iTCP:"$API_PORT" -sTCP:LISTEN >/dev/null 2>&1 || lsof -nP -iTCP:"$VITE_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "端口 ${API_PORT} 或 ${VITE_PORT} 已被占用。请先停掉旧的本地服务再启动。" >&2
  lsof -nP -iTCP:"$API_PORT" -sTCP:LISTEN 2>/dev/null || true
  lsof -nP -iTCP:"$VITE_PORT" -sTCP:LISTEN 2>/dev/null || true
  exit 1
fi

echo "Node $(node -v)，Python $("$ROOT/.venv/bin/python" -V 2>&1)"

"$ROOT/.venv/bin/python" "$ROOT/run.py" --no-window &
api_pid=$!

(
  cd "$ROOT/frontend"
  npm run dev -- --host 127.0.0.1 --port "$VITE_PORT" --strictPort
) &
vite_pid=$!

ready=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
  if ! kill -0 "$api_pid" 2>/dev/null; then
    echo "后端启动失败。" >&2
    exit 1
  fi
  if ! kill -0 "$vite_pid" 2>/dev/null; then
    echo "界面启动失败。" >&2
    exit 1
  fi
  if curl -fsS -o /dev/null --max-time 1 "${API_URL}/api/health" \
    && curl -fsS -o /dev/null --max-time 1 "${VITE_URL}/"; then
    ready=1
    break
  fi
  sleep 0.5
done

if [[ "$ready" -ne 1 ]]; then
  echo "启动超时。请查看上面的报错。" >&2
  exit 1
fi

echo "服务已启动"
echo "  界面  ${VITE_URL}"
echo "  接口  ${API_URL}"
if [[ "$(uname -s)" == "Darwin" ]]; then
  open "$VITE_URL" >/dev/null 2>&1 || true
fi

while true; do
  if ! kill -0 "$api_pid" 2>/dev/null; then
    echo "后端已退出，正在停止界面。" >&2
    exit 1
  fi
  if ! kill -0 "$vite_pid" 2>/dev/null; then
    echo "界面已退出，正在停止后端。" >&2
    exit 1
  fi
  sleep 1
done
