"""启动管理后台。"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn

from admin_app.main import create_app


def main() -> None:
    host = os.environ.get("EVC_ADMIN_HOST", "127.0.0.1")
    port = int(os.environ.get("EVC_ADMIN_PORT", "8780"))
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
