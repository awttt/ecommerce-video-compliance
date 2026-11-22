"""PyInstaller hook for numpy.

确保 NumPy 的运行时依赖在 Windows 打包时被正确包含。
numpy.typing.tests 这类测试包不进安装包。numpy.testing 会保留。
"""

import importlib.util
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

_BUNDLE = Path(__file__).resolve().parent.parent / "evc_numpy_bundle.py"
_bundle_spec = importlib.util.spec_from_file_location("evc_numpy_bundle", _BUNDLE)
_bundle = importlib.util.module_from_spec(_bundle_spec)
_bundle_spec.loader.exec_module(_bundle)
is_numpy_tests_path = _bundle.is_numpy_tests_path

# 收集 numpy 的核心模块，跳过 tests 包。
hiddenimports = [
    name for name in collect_submodules("numpy") if not is_numpy_tests_path(name)
]

# 收集动态链接库
binaries = collect_dynamic_libs('numpy')

# 确保关键模块被包含
hiddenimports += [
    'numpy',
    'numpy.core',
    'numpy.core._multiarray_umath',
    'numpy.core._dtype_ctypes',
    'numpy.core._internal',
    'numpy.core.multiarray',
    'numpy.core.umath',
    'numpy.fft',
    'numpy.linalg',
    'numpy.random',
    'numpy.ma',
    'numpy.lib',
]
