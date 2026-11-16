# -*- mode: python ; coding: utf-8 -*-
"""Windows 打包。先构建 frontend/dist，再在项目根目录执行 pyinstaller。"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent

datas = [(str(ROOT / "frontend" / "dist"), "frontend/dist")]
datas += collect_data_files("webview")
datas += collect_data_files("certifi")

hidden = (
    collect_submodules("uvicorn")
    + collect_submodules("webview")
    + [
        "aiosqlite",
        "openpyxl",
        "multipart",
        "app",
        "app.main",
        "app.factory",
        "app.desktop",
        "app.cli",
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
)

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
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="VideoCompliance",
)
