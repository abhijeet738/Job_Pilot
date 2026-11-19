"""Engine, WAL mode, table creation and sessions. Queries live in db/repo.py, not here."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine

from ..settings import get_settings
from . import tables  # noqa: F401  (imported so every table is registered on the metadata)

_engine: Engine | None = None


def _apply_pragmas(dbapi_conn: Any, _record: Any) -> None:
    """Run on every new SQLite connection."""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")  # bot, scheduler and dashboard read concurrently
    cur.execute("PRAGMA foreign_keys=ON")  # SQLite ignores foreign keys unless enabled
    cur.execute("PRAGMA busy_timeout=5000")  # wait up to 5 s for a lock instead of failing
    cur.execute("PRAGMA synchronous=NORMAL")  # safe with WAL, much faster than FULL
    cur.close()


def make_engine(db_path: Path) -> Engine:
    """Create an engine for a SQLite file, creating the parent folder if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},  # asyncio code may use another thread
    )
    event.listen(engine, "connect", _apply_pragmas)
    return engine


def get_engine() -> Engine:
    """The process-wide engine, created lazily at <data_dir>/jobpilot.db."""
    global _engine
    if _engine is None:
        _engine = make_engine(get_settings().jobpilot_data_dir / "jobpilot.db")
    return _engine


def set_engine(engine: Engine) -> None:
    """Replace the process-wide engine (tests point this at a temporary database)."""
    global _engine
    _engine = engine


def reset_engine() -> None:
    """Dispose and forget the engine so the next get_engine() builds a fresh one."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def init_db(engine: Engine | None = None) -> None:
    """Create all tables. Idempotent: existing tables and data are left untouched."""
    SQLModel.metadata.create_all(engine or get_engine())


@contextmanager
def get_session() -> Iterator[Session]:
    """Commit when the block finishes cleanly, roll back if an exception escapes.

    expire_on_commit is off so objects stay readable after the block ends.
    """
    session = Session(get_engine(), expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()