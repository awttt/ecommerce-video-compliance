"""PyInstaller hook for numpy.

确保 NumPy 的所有依赖在 Windows 打包时被正确包含。
"""

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

# 收集 numpy 的核心模块
hiddenimports = collect_submodules('numpy')

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
