# -*- mode: python ; coding: utf-8 -*-
"""桌面打包。先构建 frontend/dist，再在项目根目录执行 pyinstaller。

Windows 得到 dist/VideoCompliance/VideoCompliance.exe。
macOS 得到 dist/VideoCompliance.app。需要在对应系统上打包，不能交叉编译。
"""

import importlib.util
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

_BUNDLE = Path(SPECPATH).resolve() / "evc_numpy_bundle.py"
_bundle_spec = importlib.util.spec_from_file_location("evc_numpy_bundle", _BUNDLE)
_bundle = importlib.util.module_from_spec(_bundle_spec)
_bundle_spec.loader.exec_module(_bundle)
drop_numpy_tests = _bundle.drop_numpy_tests
is_numpy_tests_path = _bundle.is_numpy_tests_path

ROOT = Path(SPECPATH).resolve().parent

datas = [(str(ROOT / "frontend" / "dist"), "frontend/dist")]
# 管理控制台的页面一起进安装包，但它的接口和数据仍走云端后台。
console_dist = ROOT / "admin" / "web" / "dist"
if (console_dist / "index.html").is_file():
    datas.append((str(console_dist), "console/dist"))
datas += collect_data_files("webview")
datas += collect_data_files("certifi")

# 收集 OpenCV 和 NumPy 的数据文件和动态库
try:
    datas += collect_data_files("cv2")
except Exception:
    pass  # cv2 可能没有额外的数据文件

try:
    datas += [
        item
        for item in collect_data_files("numpy")
        if not is_numpy_tests_path(item[0]) and not is_numpy_tests_path(item[1])
    ]
except Exception:
    pass

hidden = collect_submodules("uvicorn") + collect_submodules("webview")

# 显式收集 OpenCV 和 NumPy 的所有子模块
try:
    hidden += collect_submodules("cv2")
except Exception:
    pass

try:
    hidden += [
        name for name in collect_submodules("numpy") if not is_numpy_tests_path(name)
    ]
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
    # 只排除 NumPy 自带的 tests 包。numpy.testing 是运行时工具，必须保留。
    # 其他第三方包里的 tests 没有逐个确认，这次不动。
    excludes=[
        "numpy.tests",
        "numpy.typing.tests",
        "numpy.core.tests",
        "numpy._core.tests",
        "numpy.f2py.tests",
        "numpy.fft.tests",
        "numpy.lib.tests",
        "numpy.linalg.tests",
        "numpy.ma.tests",
        "numpy.matrixlib.tests",
        "numpy.polynomial.tests",
        "numpy.random.tests",
        "numpy.testing.tests",
    ],
    noarchive=False,
)

for _toc_name in ("pure", "datas", "binaries"):
    _toc = getattr(a, _toc_name)
    _toc[:] = drop_numpy_tests(_toc)

# 动态库由 packaging/hooks 收集。collect_dynamic_libs 返回 (源文件, 目标目录) 二元组，
# 只能交给 hook 的 binaries。直接拼进 a.binaries 会在 COLLECT 时按三元组拆开而失败。
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
            "CFBundleName": "御镜视频审核系统",
            "CFBundleDisplayName": "御镜视频审核系统",
            "CFBundleShortVersionString": "0.1.1",
            "NSPrincipalClass": "NSApplication",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
            "NSAppTransportSecurity": {
                "NSAllowsArbitraryLoads": True,
                "NSAllowsLocalNetworking": True,
            },
        },
    )
