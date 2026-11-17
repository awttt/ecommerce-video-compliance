# -*- mode: python ; coding: utf-8 -*-
"""桌面打包。先构建 frontend/dist，再在项目根目录执行 pyinstaller。

Windows 得到 dist/VideoCompliance/VideoCompliance.exe。
macOS 得到 dist/VideoCompliance.app。需要在对应系统上打包，不能交叉编译。
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent

datas = [(str(ROOT / "frontend" / "dist"), "frontend/dist")]
datas += collect_data_files("webview")
datas += collect_data_files("certifi")

hidden = collect_submodules("uvicorn") + collect_submodules("webview")
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
    "app.core.editor",
    "app.core.aggregate",
    "app.core.parsing",
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
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
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
