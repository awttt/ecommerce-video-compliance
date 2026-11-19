from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Base

_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def reset_engine() -> None:
    global _engine, _factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _factory = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = get_settings().database_url
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args, future=True)
        if url.startswith("sqlite"):

            @event.listens_for(_engine, "connect")
            def _fk(dbapi_conn, _record) -> None:  # type: ignore[no-redef]
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

    return _engine


def get_factory() -> sessionmaker[Session]:
    global _factory
    if _factory is None:
        _factory = sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)
    return _factory


def session() -> Session:
    return get_factory()()


def get_db() -> Generator[Session, None, None]:
    db = session()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    engine = get_engine()
    Base.metadata.create_all(engine)
    columns = {column["name"] for column in inspect(engine).get_columns("tenant_members")}
    if "lifetime_cost_cents" not in columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE tenant_members ADD COLUMN lifetime_cost_cents INTEGER NOT NULL DEFAULT 0")
            )
            connection.execute(
                text("UPDATE tenant_members SET lifetime_cost_cents = month_cost_cents + previous_month_cost_cents")
            )
