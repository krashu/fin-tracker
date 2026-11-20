"""Settings validation guards (PRD §Users & access v2).

These lock the fail-loud checks the credentialed-auth surface needs: a hosted
(``cookie_secure=true``) deploy must not run on the public dev JWT secret, the
CORS allowlist can be neither a wildcard nor empty, and the token TTLs must be
positive. All construct :class:`Settings` with explicit kwargs — init args
outrank the repo-root ``.env`` the top-level conftest loads, so these are
deterministic regardless of local env.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app.core.config import _DEV_JWT_SECRET, REPO_ROOT, Settings

_VALID_CORS = "http://localhost:3000"


def test_default_jwt_secret_rejected_when_secure() -> None:
    with pytest.raises(ValidationError):
        Settings(cookie_secure=True, jwt_secret=_DEV_JWT_SECRET, cors_allowed_origins=_VALID_CORS)


def test_real_jwt_secret_ok_when_secure() -> None:
    s = Settings(
        cookie_secure=True, jwt_secret="a-real-random-secret", cors_allowed_origins=_VALID_CORS
    )
    assert s.cookie_secure is True


def test_default_secret_ok_when_not_secure() -> None:
    # Local http dev: the placeholder secret is fine while cookie_secure is off.
    s = Settings(cookie_secure=False, jwt_secret=_DEV_JWT_SECRET, cors_allowed_origins=_VALID_CORS)
    assert s.jwt_secret == _DEV_JWT_SECRET


def test_cors_wildcard_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(cors_allowed_origins="*")


def test_cors_empty_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(cors_allowed_origins="")


def test_zero_access_ttl_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(access_token_ttl_minutes=0, cors_allowed_origins=_VALID_CORS)


def test_negative_refresh_ttl_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(refresh_token_ttl_days=-1, cors_allowed_origins=_VALID_CORS)


def test_zero_rate_limit_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(auth_rate_limit_per_minute=0, cors_allowed_origins=_VALID_CORS)


def test_zero_idle_timeout_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(session_idle_timeout_minutes=0, cors_allowed_origins=_VALID_CORS)


def test_registration_closed_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0003 rule 12: past the first user, registration is closed unless the
    operator opts in. tests/conftest.py forces REGISTRATION_ENABLED=true for the
    suite, so unset it and skip the dotenv file to read the shipped default."""
    monkeypatch.delenv("REGISTRATION_ENABLED", raising=False)
    s = Settings(_env_file=None, cors_allowed_origins=_VALID_CORS)
    assert s.registration_enabled is False


def test_anchor_relative_sqlite_path_default() -> None:
    s = Settings(_env_file=None, cors_allowed_origins=_VALID_CORS)
    url = make_url(s.database_url)
    assert url.database == str((REPO_ROOT / "data" / "fin-tracker.db").resolve())


def test_anchor_relative_sqlite_path_slash_prefixed() -> None:
    s = Settings(
        _env_file=None,
        database_url="sqlite:////data/fin-tracker.db",
        cors_allowed_origins=_VALID_CORS,
    )
    url = make_url(s.database_url)
    assert url.database == str((REPO_ROOT / "data" / "fin-tracker.db").resolve())


def test_anchor_relative_sqlite_path_in_docker_container(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == "win32":
        pytest.skip("Docker container paths are POSIX-only (/data/...)")
    monkeypatch.setattr("app.core.config.REPO_ROOT", Path("/"))
    s = Settings(
        _env_file=None,
        database_url="sqlite:////data/fin-tracker.db",
        cors_allowed_origins=_VALID_CORS,
    )
    url = make_url(s.database_url)
    assert url.database == "/data/fin-tracker.db"


def test_anchor_relative_sqlite_path_preserves_memory_and_postgres() -> None:
    s_mem = Settings(
        _env_file=None, database_url="sqlite:///:memory:", cors_allowed_origins=_VALID_CORS
    )
    assert s_mem.database_url == "sqlite:///:memory:"

    pg_url = "postgresql+psycopg://user:pass@localhost:5432/fintracker"
    s_pg = Settings(_env_file=None, database_url=pg_url, cors_allowed_origins=_VALID_CORS)
    assert s_pg.database_url == pg_url


def test_db_ensure_sqlite_parent_dir(tmp_path: Path) -> None:
    from app.core.db import _ensure_sqlite_parent_dir

    target_dir = tmp_path / "deep" / "nested"
    db_file = target_dir / "test.db"
    assert not target_dir.exists()
    _ensure_sqlite_parent_dir(f"sqlite:///{db_file}")
    assert target_dir.exists()


def test_make_engine_creates_missing_sqlite_parent_dir(tmp_path: Path) -> None:
    from app.core.db import make_engine

    target_dir = tmp_path / "engine_data"
    db_file = target_dir / "test.db"
    assert not target_dir.exists()
    eng = make_engine(f"sqlite:///{db_file}")
    assert target_dir.exists()
    with eng.connect() as conn:
        res = conn.exec_driver_sql("SELECT 1").scalar()
        assert res == 1
    assert db_file.exists()
    eng.dispose()
