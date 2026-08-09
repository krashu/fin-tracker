"""Startup auto-migration: :func:`app.core.migrations.run_migrations`.

Two behaviors are locked here:

* flag ON (the local-first default) brings a fresh DB to ``head`` — this is
  the fix for a stale dev DB 500-ing on a table a pulled migration added;
* flag OFF is a true no-op (never calls ``command.upgrade``) — the posture the
  test suite and the v2 hosted deploy rely on.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core import migrations
from app.core.config import Settings
from app.core.demo import DEMO_EMAIL, DEMO_PASSWORD
from app.core.security import verify_password
from app.models import User


def _settings(*, url: str, enabled: bool) -> Settings:
    # Field names (not the UPPERCASE env aliases) so the pydantic-settings init
    # type-checks; populate_by_name accepts either at runtime.
    return Settings(database_url=url, apply_migrations_on_startup=enabled)


def test_run_migrations_upgrades_fresh_db_to_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "startup.db"
    settings = _settings(url=f"sqlite:///{db_path}", enabled=True)
    # env.py sources the URL from get_settings(); point it at the temp DB.
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)

    migrations.run_migrations(settings)

    conn = sqlite3.connect(db_path)
    try:
        head = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()

    assert head is not None
    # Tables added by the migrations the stale dev DB was missing.
    assert {"fx_rates", "instruments"} <= tables


def test_migrated_demo_user_can_authenticate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The migrated demo row's frozen hash must still match DEMO_PASSWORD — the ONLY
    path that exercises migration 0017's inlined ``_DEMO_PASSWORD_HASH``.

    Every other auth test re-hashes DEMO_PASSWORD in a fixture or via register_user,
    so a stale inlined hash, a changed DEMO_PASSWORD, a UUID/email-stamp regression in
    the 0017 UPDATE, or a broken both-or-neither invariant would leave the whole suite
    green while "Try the demo" 401s on an operator's box that opted in. Reading the row
    through the ORM (as login does) and verifying the password end-to-end closes that gap.

    Deliberately checks ``verify_password`` rather than ``authenticate``: whether those
    creds are ACCEPTED is a runtime policy question (``demo_login_permitted``, off by
    default) that belongs in the auth tests. This one asks only whether the credential
    the migration stamped is the credential the app believes it stamped.
    """
    db_path = tmp_path / "demo.db"
    settings = _settings(url=f"sqlite:///{db_path}", enabled=True)
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)

    migrations.run_migrations(settings)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with Session(engine) as s:
            user = s.get(User, settings.v1_user_id)
            assert user is not None, "migration 0017 did not stamp the demo row"
            assert user.email == DEMO_EMAIL
            assert user.password_hash is not None  # both-or-neither invariant
            assert verify_password(DEMO_PASSWORD, user.password_hash), (
                "migration 0017 _DEMO_PASSWORD_HASH no longer matches DEMO_PASSWORD"
            )
    finally:
        engine.dispose()


def test_run_migrations_creates_missing_sqlite_parent_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fresh checkout has no ./data dir; SQLite can't open a DB file in a missing
    # directory. run_migrations must mkdir it before connecting.
    db_path = tmp_path / "data" / "startup.db"
    assert not db_path.parent.exists()
    settings = _settings(url=f"sqlite:///{db_path}", enabled=True)
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)

    migrations.run_migrations(settings)

    assert db_path.exists()


def test_run_migrations_disabled_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def _fail_upgrade(*_a: object, **_k: object) -> None:
        nonlocal called
        called = True

    # String target: patch the name as looked up in app.core.migrations, avoiding a
    # direct `migrations.command` reference (not an explicitly re-exported attribute).
    monkeypatch.setattr("app.core.migrations.command.upgrade", _fail_upgrade)
    migrations.run_migrations(_settings(url="sqlite:///./unused.db", enabled=False))

    assert called is False
