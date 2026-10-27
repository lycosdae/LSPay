from contextlib import contextmanager

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool
    return create_engine(url, future=True, **kwargs)


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def configure(url: str) -> None:
    """Re-point the app at another database (used by tests)."""
    global engine
    engine = make_engine(url)
    SessionLocal.configure(bind=engine)


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(engine)
    add_missing_columns()


def add_missing_columns() -> None:
    """Add columns that are new in the models to tables created by an older version.

    Only plain additions are handled; every new column must have a server_default.
    """
    with engine.begin() as conn:
        insp = inspect(conn)
        for table in Base.metadata.sorted_tables:
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                if col.server_default is None:
                    raise RuntimeError(f"{table.name}.{col.name} needs a server_default to be added")
                col_type = col.type.compile(dialect=conn.dialect)
                default = col.server_default.arg
                if isinstance(default, str):
                    default_sql = "'" + default.replace("'", "''") + "'"
                else:
                    default_sql = str(default.compile(dialect=conn.dialect))
                conn.exec_driver_sql(
                    f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col_type} NOT NULL DEFAULT {default_sql}"
                )


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
