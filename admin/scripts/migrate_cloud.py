"""把旧单库里的租户、成员、额度数据搬到云端库。

本机库的审核任务和词库保持不动。目标库已有数据时默认拒绝，避免覆盖线上数据。

用法：
  EVC_ADMIN_CLOUD_DATABASE_URL='postgresql+psycopg://user:pass@host:5432/evc_admin' \
    python scripts/migrate_cloud.py --source sqlite:///data/admin.db --dry-run

确认无误后去掉 --dry-run。执行前先停掉管理后台，避免迁移过程中又有新写入。
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from admin_app.db import CLOUD_MODELS  # noqa: E402
from admin_app.models import Base  # noqa: E402


def _engine(url: str):
    return create_engine(url, future=True, pool_pre_ping=not url.startswith("sqlite"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=f"sqlite:///{ROOT / 'data' / 'admin.db'}")
    parser.add_argument("--target", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    import os

    target_url = args.target or os.environ.get("EVC_ADMIN_CLOUD_DATABASE_URL", "").strip()
    if not target_url:
        print("缺少目标库：传 --target，或设置 EVC_ADMIN_CLOUD_DATABASE_URL")
        return 2
    if target_url == args.source:
        print("源库和目标库相同，不需要迁移")
        return 2

    source = _engine(args.source)
    target = _engine(target_url)
    source_tables = set(inspect(source).get_table_names())
    Base.metadata.create_all(target, tables=[model.__table__ for model in CLOUD_MODELS])

    plan: list[tuple[str, list[dict]]] = []
    for model in CLOUD_MODELS:
        name = model.__tablename__
        if name not in source_tables:
            continue
        with source.connect() as connection:
            rows = [dict(row._mapping) for row in connection.execute(select(model.__table__))]
        plan.append((name, rows))

    busy: list[str] = []
    with target.connect() as connection:
        for model in CLOUD_MODELS:
            if connection.execute(select(model.__table__).limit(1)).first() is not None:
                busy.append(model.__tablename__)
    if busy:
        print("目标库已有数据，先清空或换一个库：" + "、".join(busy))
        return 3

    total = 0
    for name, rows in plan:
        total += len(rows)
        print(f"{name}: {len(rows)} 行")
    if args.dry_run:
        print(f"dry-run：共 {total} 行，没有写入")
        return 0

    # 按外键顺序写入：CLOUD_MODELS 本身就是父表在前。
    with target.begin() as connection:
        for name, rows in plan:
            if rows:
                model = next(item for item in CLOUD_MODELS if item.__tablename__ == name)
                connection.execute(model.__table__.insert(), rows)
    print(f"已写入 {total} 行到 {target_url.split('@')[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
