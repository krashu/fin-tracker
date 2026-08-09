"""Startup demo-seed gate: :func:`app.main._maybe_seed_demo`.

Locks the lifespan behaviour around the direct-DB seeder (the seeder's own row
output is covered in ``tests/services/test_demo_seed.py``):

* flag ON + empty DB → seeds once;
* flag ON + already-populated DB → no-op (the strict AND empty-check prevents a
  re-seed that would duplicate rows);
* flag OFF → never touches the DB.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from app.core.config import Settings, get_settings
from app.core.db import make_engine
from app.models import Account, Base, User
from app.services.provisioning import provision_default_categories


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
def session_factory(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> sessionmaker[Session]:
    """Factory bound to the in-memory engine, wired in as ``app.main.SessionLocal``
    so ``_maybe_seed_demo`` hits the test DB instead of the real file DB."""
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", factory)
    return factory


@pytest.fixture
def seeded_user(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as s:
        user = User(id=get_settings().v1_user_id)
        s.add(user)
        s.flush()  # user row must exist before its categories' FK
        provision_default_categories(s, user.id)
        s.commit()


def _account_count(factory: sessionmaker[Session]) -> int:
    with factory() as s:
        return s.scalar(select(func.count()).select_from(Account)) or 0


def test_enabled_empty_db_seeds(session_factory: sessionmaker[Session], seeded_user: None) -> None:
    main._maybe_seed_demo(Settings(SEED_DEMO_ON_STARTUP=True))
    assert _account_count(session_factory) == 2


def test_enabled_populated_db_is_noop(
    session_factory: sessionmaker[Session], seeded_user: None
) -> None:
    settings = Settings(SEED_DEMO_ON_STARTUP=True)
    main._maybe_seed_demo(settings)
    first = _account_count(session_factory)
    # Second run must short-circuit on the empty-check — no duplicate rows, no
    # IntegrityError from re-inserting identical fingerprints.
    main._maybe_seed_demo(settings)
    assert _account_count(session_factory) == first == 2


def test_disabled_is_noop(session_factory: sessionmaker[Session], seeded_user: None) -> None:
    main._maybe_seed_demo(Settings(SEED_DEMO_ON_STARTUP=False))
    assert _account_count(session_factory) == 0
