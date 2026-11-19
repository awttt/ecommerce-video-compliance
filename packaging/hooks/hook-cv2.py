"""PyInstaller hook for opencv-python-headless.

确保 OpenCV 的所有依赖在 Windows 打包时被正确包含。
"""

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

# 收集 cv2 的所有子模块
hiddenimports = collect_submodules('cv2')

# 收集数据文件（配置文件、模型等）
datas = collect_data_files('cv2', include_py_files=False)

# 收集动态链接库（Windows 上的 .pyd 和 .dll 文件）
binaries = collect_dynamic_libs('cv2')

# 确保核心模块被包含
hiddenimports += [
    'cv2',
    'cv2.cv2',
    'numpy',
    'numpy.core',
    'numpy.core._multiarray_umath',
]
