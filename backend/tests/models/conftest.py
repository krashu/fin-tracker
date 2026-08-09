"""Model-layer test fixtures.

Builds a fresh in-memory SQLite engine per test, reusing :func:`app.core.db.make_engine`
so the FK-enforcement event listener is wired exactly like production. Tables are
created via ``Base.metadata.create_all`` (no Alembic for model unit tests — the
migration story is exercised in a separate suite when Alembic lands).

``poolclass=StaticPool`` matches ``tests/api/conftest.py`` and
``tests/services/conftest.py``: without it SQLAlchemy's default pool can hand a
second checkout its OWN ``:memory:`` database, so anything reading over a second
connection sees an empty schema. This directory was the last one missing it.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import make_engine
from app.models import Base


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
