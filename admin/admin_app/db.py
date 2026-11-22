"""两套库：本机放审核工具数据，云端放租户、成员、额度。

配置了 EVC_ADMIN_CLOUD_DATABASE_URL 时按模型拆到两个引擎；没配置时两边共用一个库，
和旧版本行为一致，测试和单机演示都不用改。
"""

from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from admin_app.config import get_settings
from admin_app.models import (
    AppMeta,
    AppSetting,
    AuditLog,
    Base,
    Device,
    Invite,
    Member,
    MemberToken,
    QuotaAllocation,
    CompanyRule,
    CompanyRuleSubmission,
    RelayAccount,
    RelayLedger,
    SessionToken,
    SharedRule,
    Tenant,
    UsageSnapshot,
)

# 云端库：登录、成员、席位、额度、用量和审计。
CLOUD_MODELS = (
    Tenant,
    Member,
    Invite,
    Device,
    MemberToken,
    SessionToken,
    QuotaAllocation,
    UsageSnapshot,
    AuditLog,
    AppSetting,
    AppMeta,
    RelayAccount,
    RelayLedger,
    SharedRule,
    CompanyRule,
    CompanyRuleSubmission,
)
# 词库审批已去掉。公司规则在云端库，本机库不再单独建表。
LOCAL_MODELS: tuple[type, ...] = ()

_local_engine: Engine | None = None
_cloud_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def reset_engine() -> None:
    global _local_engine, _cloud_engine, _factory
    for engine in (_local_engine, _cloud_engine):
        if engine is not None:
            engine.dispose()
    _local_engine = None
    _cloud_engine = None
    _factory = None


def _make_engine(url: str) -> Engine:
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args, future=True, pool_pre_ping=not url.startswith("sqlite"))
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _fk(dbapi_conn, _record) -> None:  # type: ignore[no-redef]
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def get_engine() -> Engine:
    global _local_engine
    if _local_engine is None:
        _local_engine = _make_engine(get_settings().database_url)
    return _local_engine


def get_cloud_engine() -> Engine | None:
    """没填云端地址就返回 None，表示和本机库共用一个库。"""
    global _cloud_engine
    url = get_settings().cloud_database_url
    if not url:
        return None
    if _cloud_engine is None:
        _cloud_engine = _make_engine(url)
    return _cloud_engine


def get_factory() -> sessionmaker[Session]:
    global _factory
    if _factory is None:
        local = get_engine()
        cloud = get_cloud_engine()
        if cloud is None:
            _factory = sessionmaker(bind=local, autoflush=False, expire_on_commit=False)
        else:
            # 未登记的模型默认走本机库；租户、成员、额度统一路由到云端。
            _factory = sessionmaker(
                bind=local,
                binds={model: cloud for model in CLOUD_MODELS},
                autoflush=False,
                expire_on_commit=False,
            )
    return _factory


def session() -> Session:
    return get_factory()()


def get_db() -> Generator[Session, None, None]:
    db = session()
    try:
        yield db
    finally:
        db.close()


# create_all 不会给已有表补列。这里只做加法，不改动已有数据。
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "tenant_members": {
        "lifetime_cost_cents": "INTEGER NOT NULL DEFAULT 0",
        "relay_balance_cents": "INTEGER NOT NULL DEFAULT 0",
        "relay_status": "VARCHAR(16) NOT NULL DEFAULT ''",
        "relay_checked_at": "DATETIME",
        "relay_api_key": "TEXT NOT NULL DEFAULT ''",
        "relay_access_token": "TEXT NOT NULL DEFAULT ''",
        "relay_refresh_token": "TEXT NOT NULL DEFAULT ''",
        "relay_token_expires_at": "DATETIME",
    },
    "review_jobs": {
        "engine_id": "VARCHAR(32) NOT NULL DEFAULT ''",
        "started_by": "INTEGER",
    },
}


def _add_missing_columns(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table, added in _ADDED_COLUMNS.items():
            if table not in tables:
                continue
            columns = {column["name"] for column in inspector.get_columns(table)}
            for name, ddl in added.items():
                if name in columns:
                    continue
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                if table == "tenant_members" and name == "lifetime_cost_cents":
                    connection.execute(
                        text("UPDATE tenant_members SET lifetime_cost_cents = month_cost_cents + previous_month_cost_cents")
                    )


def ensure_shared_rules(engine: Engine) -> None:
    """在已有库上补 shared_rules。不插入数据，也不依赖演示账号。"""
    SharedRule.__table__.create(engine, checkfirst=True)
    for index in SharedRule.__table__.indexes:
        index.create(engine, checkfirst=True)


def _drop_legacy_rule_submissions(engine: Engine) -> None:
    """旧表一行是整套规则。改成一条改动一行后，没有 op 列的空壳删掉再建。"""
    inspector = inspect(engine)
    if "company_rule_submissions" not in set(inspector.get_table_names()):
        return
    columns = {column["name"] for column in inspector.get_columns("company_rule_submissions")}
    if "op" in columns:
        return
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE company_rule_submissions"))


def init_db() -> None:
    local = get_engine()
    cloud = get_cloud_engine()
    if cloud is None:
        _drop_legacy_rule_submissions(local)
        Base.metadata.create_all(local)
        _add_missing_columns(local)
        ensure_shared_rules(local)
        return
    _drop_legacy_rule_submissions(cloud)
    Base.metadata.create_all(local, tables=[model.__table__ for model in LOCAL_MODELS])
    Base.metadata.create_all(cloud, tables=[model.__table__ for model in CLOUD_MODELS])
    _add_missing_columns(local)
    _add_missing_columns(cloud)
    ensure_shared_rules(cloud)