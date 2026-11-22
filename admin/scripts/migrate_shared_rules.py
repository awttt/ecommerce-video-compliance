"""在已有管理库上创建 shared_rules。

不读取、不写入演示数据。生产库只要已经有管理后台的表，执行一次即可。
重复执行不会清数据，也不会插行。

用法：
  cd admin
  EVC_ADMIN_CLOUD_DATABASE_URL='postgresql+psycopg://user:pass@host:5432/evc_admin' \
    python scripts/migrate_shared_rules.py

没配云端地址时，改的是本机 EVC_ADMIN_DATABASE_URL（或默认的 admin/data/admin.db）。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, inspect  # noqa: E402

from admin_app.db import ensure_shared_rules  # noqa: E402


def main() -> int:
    import os

    url = os.environ.get("EVC_ADMIN_CLOUD_DATABASE_URL", "").strip()
    if not url:
        url = os.environ.get("EVC_ADMIN_DATABASE_URL", "").strip()
    if not url:
        default = ROOT / "data" / "admin.db"
        default.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{default}"
    engine = create_engine(url, future=True, pool_pre_ping=not url.startswith("sqlite"))
    ensure_shared_rules(engine)
    tables = set(inspect(engine).get_table_names())
    if "shared_rules" not in tables:
        print("shared_rules 没有建出来")
        return 1
    print("shared_rules 已就绪")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
