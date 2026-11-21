#!/usr/bin/env bash
# 在 Apple 芯片或 Intel 的本机上打出可双击的 macOS 应用。
# 用法：scripts/build_mac.sh
# 不能在 Mac 上打出 Windows exe。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "这个脚本只在 macOS 上运行。Windows 用 scripts/build_windows.ps1。" >&2
  exit 1
fi

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

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "需要先安装 Python 3.11 或更高版本。" >&2
    exit 1
  fi
  python3 -m venv "$ROOT/.venv"
fi

PY="$ROOT/.venv/bin/python"
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "虚拟环境里的 Python 低于 3.11。" >&2
  exit 1
fi

if ! load_node; then
  echo "需要 Node.js 18 或更高版本。可执行：nvm install 22" >&2
  exit 1
fi

"$PY" -m pip install --upgrade pip
"$PY" -m pip install -r requirements.txt pyinstaller pyobjc-framework-Cocoa pyobjc-framework-WebKit

(
  cd "$ROOT/frontend"
  npm install
  npm run build
)

if [[ ! -f "$ROOT/frontend/dist/index.html" ]]; then
  echo "frontend/dist 没有构建出来，已停止打包。" >&2
  exit 1
fi

# 管理控制台页面随安装包分发，接口仍请求云端后台。
(
  cd "$ROOT/admin/web"
  npm install
  npm run build
)

if [[ ! -f "$ROOT/admin/web/dist/index.html" ]]; then
  echo "admin/web/dist 没有构建出来，已停止打包。" >&2
  exit 1
fi

"$PY" -m PyInstaller --noconfirm --clean packaging/evc.spec

APP="$ROOT/dist/VideoCompliance.app"
if [[ ! -d "$APP" ]]; then
  echo "没有生成 $APP" >&2
  exit 1
fi

# 本机测试用临时签名。发给别人仍可能被 Gatekeeper 拦住。
codesign --force --deep --sign - "$APP"
xattr -cr "$APP" || true

echo ""
echo "打包完成：$APP"
echo "双击即可打开。数据在 ~/Library/Application Support/EcommerceVideoCompliance ，和项目里的 data/ 不是同一份。"
echo "FFmpeg 用 Homebrew 安装后可直接用，也可以把 ffmpeg 和 ffprobe 放在 .app 旁边。"
echo "默认端口 8765。这个端口被占用时，应用起不来，日志在上面的数据目录里。"
