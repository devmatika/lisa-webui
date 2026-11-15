"""Engine, sessions and migrations for the Lisa Enterprise database.

``LISA_DATABASE_URL`` (a secret: it carries the DB password) selects the database, e.g.
``postgresql+psycopg://lisa:<pw>@postgres:5432/lisa``. Migrations are Alembic revisions in
``migrations/versions`` and run once at WebUI start (``migrate()``), inside a transaction, under a
Postgres advisory lock so two replicas never migrate concurrently.
"""
from __future__ import annotations

import contextlib
import os
import threading
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_MIGRATIONS = Path(__file__).with_name("migrations")
_LOCK = threading.Lock()
_engine: Engine | None = None
_factory: sessionmaker | None = None
_MIGRATION_LOCK_ID = 7_514_220_001  # pg_advisory_xact_lock key for "lisa enterprise migrate"


def database_url() -> str:
    url = os.environ.get("LISA_DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("LISA_ENTERPRISE is on but LISA_DATABASE_URL is not set")
    return url


def engine() -> Engine:
    global _engine, _factory
    with _LOCK:
        if _engine is None:
            url = database_url()
            kwargs = {"pool_pre_ping": True, "future": True}
            if url.startswith("postgresql"):
                kwargs.update(pool_size=5, max_overflow=10, pool_recycle=1800)
            _engine = create_engine(url, **kwargs)
            _factory = sessionmaker(bind=_engine, expire_on_commit=False)
        return _engine


@contextlib.contextmanager
def session() -> Iterator[Session]:
    """One unit of work: commit on success, roll back on any exception."""
    engine()
    db = _factory()
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def migrate() -> str:
    """Upgrade the schema to head; returns the revision now applied."""
    from alembic import command
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext

    eng = engine()
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS))
    with eng.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _MIGRATION_LOCK_ID})
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
        return MigrationContext.configure(conn).get_current_revision() or ""


def reset_for_tests() -> None:
    """Drop the cached engine so a test can point LISA_DATABASE_URL elsewhere."""
    global _engine, _factory
    with _LOCK:
        if _engine is not None:
            _engine.dispose()
        _engine = _factory = None
