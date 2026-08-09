"""Service-layer test fixtures.

One home for the ``engine`` / ``session`` / ``user`` trio that all 14
``tests/services`` modules previously declared for themselves — 37 definitions
that were byte-identical, verified by hashing each body before the hoist.
``tests/``, ``tests/api/``, ``tests/models/`` and ``tests/parsers/`` each already
had a ``conftest.py``; this directory was the one that did not, and the drift that
predicts had already started elsewhere (``tests/models/conftest.py`` never received
the ``StaticPool`` that ``tests/api/conftest.py`` documents as load-bearing).

Fixtures that are genuinely per-module stay in their own file — ``user_id``
(test_backup), ``seeded_user`` (test_demo_seed) and ``seeded`` (test_import_service)
each build different state and are NOT hoisted here.

``expire_on_commit=False`` matches what all 14 modules already used, but it has a
sharp edge worth knowing before writing an assertion: attributes stay live in the
identity map after ``commit()``, so reading a column back through THIS session
returns the Python value the test wrote, not what the DB would hand back. Where the
round-trip itself is the contract — notably ``DateTime``, which SQLite returns
**naive** — the read has to cross a session boundary to mean anything.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.core.db import make_engine
from app.models import Base, User


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        Base.metadata.drop_all(eng)
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = factory()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def user(session: Session) -> User:
    u = User(id=get_settings().v1_user_id)
    session.add(u)
    session.flush()
    return u


@pytest.fixture
def fresh_session(engine: Engine) -> Iterator[Session]:
    """A SECOND session on the same engine — the only way to read a write back.

    Shares the ``StaticPool`` connection with ``session`` (same in-memory DB) but
    has its own identity map, so a committed row must be re-SELECTed instead of
    being served from memory. Required by any test whose subject is what the DB
    actually stored — see ``test_datetime_boundary``.
    """
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = factory()
    try:
        yield s
    finally:
        s.close()
