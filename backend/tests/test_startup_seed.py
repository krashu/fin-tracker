"""Startup demo-seed sync: :func:`app.main._maybe_seed_demo`.

Locks the lifespan behaviour around the direct-DB seeder (the seeder's own row
output, including how it rolls the window forward across a later boot, is
covered in ``tests/services/test_demo_seed.py``):

* flag ON + empty DB → seeds;
* flag ON + already-populated DB, same day → re-syncs without duplicating rows
  (the seeder wipes-and-regenerates its own accounts' transactions rather than
  skipping — there is no empty-DB gate here anymore);
* flag OFF → never touches the DB.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from app.core import clock
from app.core.config import Settings, get_settings
from app.core.db import make_engine
from app.models import Account, Base, Transaction, User
from app.services.provisioning import provision_default_categories

# Fixed so two calls in the same test see the same `clock.today()` — real
# wall-clock could tick over a day boundary between them, which would make the
# "same-day re-sync doesn't duplicate" assertion flaky rather than deterministic.
_ANCHOR = date(2026, 8, 20)


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


def _transaction_count(factory: sessionmaker[Session]) -> int:
    with factory() as s:
        return s.scalar(select(func.count()).select_from(Transaction)) or 0


def test_enabled_empty_db_seeds(session_factory: sessionmaker[Session], seeded_user: None) -> None:
    main._maybe_seed_demo(Settings(SEED_DEMO_ON_STARTUP=True))
    assert _account_count(session_factory) == 2


def test_enabled_populated_db_resyncs_without_duplicating(
    session_factory: sessionmaker[Session], seeded_user: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(clock, "today", lambda: _ANCHOR)
    settings = Settings(SEED_DEMO_ON_STARTUP=True)
    main._maybe_seed_demo(settings)
    first_accounts = _account_count(session_factory)
    first_txns = _transaction_count(session_factory)
    # Second run, same anchor: the seeder wipes-and-regenerates its own demo
    # accounts' transactions rather than skipping — must land on the same
    # counts, not double them or raise on a duplicate fingerprint.
    main._maybe_seed_demo(settings)
    assert _account_count(session_factory) == first_accounts == 2
    assert _transaction_count(session_factory) == first_txns


def test_disabled_is_noop(session_factory: sessionmaker[Session], seeded_user: None) -> None:
    main._maybe_seed_demo(Settings(SEED_DEMO_ON_STARTUP=False))
    assert _account_count(session_factory) == 0
