"""桌面程序入口。开发时 `python run.py`，打包时也用这个文件。"""

from __future__ import annotations

import sys

from app.desktop import main

if __name__ == "__main__":
    if sys.version_info < (3, 11):
        sys.stderr.write("需要 Python 3.11 或更高版本\n")
        raise SystemExit(1)
    main()
