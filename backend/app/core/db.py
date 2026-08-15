"""SQLAlchemy engine + session factory.

One module-level :class:`~sqlalchemy.engine.Engine` per process, lazily
constructed from :func:`app.core.config.get_settings`. Sessions come from
:data:`SessionLocal`; routes take one via the :func:`get_db` FastAPI
dependency, which never commits.

SQLite needs ``foreign_keys=ON`` explicitly per-connection (off by default
in libsqlite3 for back-compat). The ``connect`` event listener enables it
so ``ON DELETE`` cascades and FK constraint failures actually fire.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import Pool

from app.core.config import get_settings


def make_engine(url: str, poolclass: type[Pool] | None = None) -> Engine:
    """Build a SQLAlchemy engine, enforcing SQLite FKs when the URL is sqlite.

    Used to construct the module-level :data:`engine` from settings and reused
    by the test suite to build in-memory engines that share the same
    FK-enforcement behaviour as the live database. The optional ``poolclass``
    knob exists for tests that run requests across multiple threads against
    ``sqlite:///:memory:`` — passing :class:`~sqlalchemy.pool.StaticPool`
    keeps every connection pointed at the same in-memory database.
    """
    # SQLite-specific: allow the connection to cross thread boundaries
    # (FastAPI's async event loop dispatches sync DB work to a thread pool)
    # and pin foreign-key enforcement at connect-time.
    connect_args: dict[str, Any] = {"check_same_thread": False} if url.startswith("sqlite") else {}
    create_kwargs: dict[str, Any] = {"connect_args": connect_args}
    if poolclass is not None:
        create_kwargs["poolclass"] = poolclass
    engine = create_engine(url, **create_kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _configure_sqlite(dbapi_connection: Any, _conn_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return engine


engine: Engine = make_engine(get_settings().database_url)
# autoflush=False: services flush explicitly so read-heavy endpoints don't
#   trigger surprise writes mid-query. Diverges from SA default (True).
# expire_on_commit=False: ORM attributes survive commit() so view layers can
#   read fields after the txn closes without a re-SELECT (SA docs recipe for
#   web frameworks).
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session]:
    """FastAPI dependency: yields a session, closes on teardown. **Does not commit.**

    Routes that mutate state must call ``session.commit()`` explicitly before
    returning — this matches the FastAPI tutorial pattern and keeps commit
    points visible at the handler level. Services never open a session of
    their own: they take one as a parameter, flush, and leave the commit to
    the route.
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
