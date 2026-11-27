"""御镜视频审核系统。本地桌面应用。"""

from app.core.cpu_limits import limit_native_threads

# numpy / opencv 在导入时读取这些变量，必须先于它们。
limit_native_threads()

__version__ = "0.1.9"
