from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker


def now():
    """All database timestamps are naive UTC, including SQLite."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def uid():
    return str(uuid4())


class Base(DeclarativeBase):
    pass


def make_engine(url):
    url = make_url(url)
    sqlite = url.get_backend_name() == "sqlite"
    if sqlite:
        Path("data").mkdir(exist_ok=True)
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False, "timeout": 30} if sqlite else {},
        pool_pre_ping=True,
    )
    if sqlite:

        @event.listens_for(engine, "connect")
        def sqlite_config(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")

    return engine


def sessions(engine):
    return sessionmaker(engine, expire_on_commit=False)


def schema_gaps(engine):
    """Inspect required tables and columns without changing schema or application data."""
    from . import models  # noqa: F401 — register all mapped tables before inspecting them

    with engine.connect() as connection:
        columns = inspect(connection).get_multi_columns(filter_names=list(Base.metadata.tables))
    missing_tables, missing_columns = [], {}
    for table in Base.metadata.sorted_tables:
        actual = columns.get((table.schema, table.name))
        if actual is None:
            missing_tables.append(table.name)
            continue
        missing = sorted(set(table.columns.keys()) - {column["name"] for column in actual})
        if missing:
            missing_columns[table.name] = missing
    return {"missing_tables": sorted(missing_tables), "missing_columns": missing_columns}
