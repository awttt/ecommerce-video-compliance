# -*- mode: python ; coding: utf-8 -*-
"""桌面打包。先构建 frontend/dist，再在项目根目录执行 pyinstaller。

Windows 得到 dist/VideoCompliance/VideoCompliance.exe。
macOS 得到 dist/VideoCompliance.app。需要在对应系统上打包，不能交叉编译。
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_dynamic_libs

ROOT = Path(SPECPATH).resolve().parent

datas = [(str(ROOT / "frontend" / "dist"), "frontend/dist")]
datas += collect_data_files("webview")
datas += collect_data_files("certifi")

# 收集 OpenCV 和 NumPy 的数据文件和动态库
try:
    datas += collect_data_files("cv2")
except Exception:
    pass  # cv2 可能没有额外的数据文件

try:
    datas += collect_data_files("numpy")
except Exception:
    pass

hidden = collect_submodules("uvicorn") + collect_submodules("webview")

# 显式收集 OpenCV 和 NumPy 的所有子模块
try:
    hidden += collect_submodules("cv2")
except Exception:
    pass

try:
    hidden += collect_submodules("numpy")
except Exception:
    pass

hidden += [
    "aiosqlite",
    "openpyxl",
    "multipart",
    "app",
    "app.main",
    "app.factory",
    "app.desktop",
    "app.cli",
    "app.mcp_server",
    "app.api.routes",
    "app.services.manager",
    "app.core.pipeline",
    "app.core.deepseek",
    "app.core.ffmpeg",
    "app.core.flash",
    "app.core.motion",
    "cv2",
    "numpy",
    "numpy.core",
    "numpy.core._multiarray_umath",
    "numpy.fft",
    "numpy.linalg",
    "numpy.random",
    "app.core.editor",
    "app.core.aggregate",
    "app.core.parsing",
    "app.core.flash_tuning",
    "app.core.flash_diagnostics",
    "certifi",
]
if sys.platform == "darwin":
    hidden += [
        "webview.platforms.cocoa",
        "AppKit",
        "Foundation",
        "WebKit",
        "objc",
        "PyObjCTools",
        "PyObjCTools.AppHelper",
    ]

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[str(ROOT / "packaging" / "hooks")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

# 收集 OpenCV 和 NumPy 的动态库（Windows 需要 .pyd 和 .dll 文件）
try:
    cv2_binaries = collect_dynamic_libs("cv2")
    a.binaries += cv2_binaries
except Exception as e:
    print(f"警告：无法收集 cv2 动态库：{e}")

try:
    numpy_binaries = collect_dynamic_libs("numpy")
    a.binaries += numpy_binaries
except Exception as e:
    print(f"警告：无法收集 numpy 动态库：{e}")
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VideoCompliance",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=(str(ROOT / "packaging" / "app.ico") if sys.platform == "win32" else None),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="VideoCompliance",
)
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="VideoCompliance.app",
        icon=None,
        bundle_identifier="local.ecommerce.videocompliance",
        info_plist={
            "CFBundleName": "御镜视频素材审核系统",
            "CFBundleDisplayName": "御镜视频素材审核系统",
            "CFBundleShortVersionString": "0.1.0",
            "NSPrincipalClass": "NSApplication",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
            "NSAppTransportSecurity": {
                "NSAllowsArbitraryLoads": True,
                "NSAllowsLocalNetworking": True,
            },
        },
    )
