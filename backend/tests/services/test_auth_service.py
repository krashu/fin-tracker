"""auth_service unit tests (PRD §Users & access v2).

Service-level (own in-memory engine, no TestClient) coverage for the behaviors
that the HTTP suite can't drive deterministically: the concurrent-duplicate
registration race, the hardened-deploy demo-login gate, and password-verify
robustness.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import clock, demo
from app.core.config import Settings
from app.core.security import verify_password
from app.models import RefreshSession, User
from app.services import auth_service

_PW = "correct horse battery"

# These tests open their own `Session(engine)` blocks rather than taking the shared
# `session` fixture — several of them need two concurrent sessions on one database, or
# have to read a write back across a session boundary. They still take the shared
# `engine` fixture (tests/services/conftest.py) so the connection is disposed on
# teardown and FK enforcement matches the live database: the module used to build its
# own bare `create_engine("sqlite://")`, which leaked a sqlite3 connection per test
# (the suite's `ResourceWarning: unclosed database`, attributed by GC timing to
# unrelated tests further down the run) and silently ran without `make_engine`'s
# `PRAGMA foreign_keys=ON`.


def test_register_duplicate_email_race(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """The concurrent-duplicate race (pre-check passes, a competing register commits
    during the argon2 hash window) must surface the flush() IntegrityError as
    EmailAlreadyExistsError → 409, not a raw IntegrityError → 500. Guards that
    flush()/provision/commit all sit inside the try."""
    orig_hash = auth_service.hash_password

    def racing_hash(pw: str) -> str:
        # Request A commits the same email during B's hashing window, then we
        # restore so only this first call races.
        with Session(engine) as a:
            a.add(User(email="race@example.com", password_hash="competing"))
            a.commit()
        monkeypatch.setattr(auth_service, "hash_password", orig_hash)
        return orig_hash(pw)

    monkeypatch.setattr(auth_service, "hash_password", racing_hash)
    with Session(engine) as b, pytest.raises(auth_service.EmailAlreadyExistsError):
        auth_service.register_user(b, email="race@example.com", password=_PW)


def test_register_non_email_integrity_error_is_not_mislabeled(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A constraint failure unrelated to the email index — e.g. Phase A5's seed-dictionary
    inserts colliding on something other than uq_users_email — must surface as the original
    IntegrityError, not get mislabeled EmailAlreadyExistsError. Direct test of the narrowed
    ``except IntegrityError`` handler (trap 4): the comment it replaced asserted email was the
    only reachable IntegrityError in that block, which adding the seed-dictionary inserts made
    false."""

    def _boom(_session: Session, _user_id: object) -> None:
        raise IntegrityError(
            "INSERT INTO merchant_alias ...",
            {},
            BaseException(
                "UNIQUE constraint failed: merchant_alias.user_id, merchant_alias.pattern"
            ),
        )

    monkeypatch.setattr(auth_service, "provision_seed_merchant_dictionary", _boom)
    with Session(engine) as s, pytest.raises(IntegrityError):
        auth_service.register_user(s, email="nonemail@example.com", password=_PW)


def test_authenticate_rejects_demo_login_when_secure(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a hardened (cookie_secure) deploy the public demo credentials must not
    authenticate, while a normal user still can.

    Both flag positions, because ``demo_login_permitted`` ANDs the two signals: an
    operator who explicitly sets DEMO_LOGIN_ENABLED on an https deploy must STILL be
    refused. That second case is the reason the flag was not allowed to replace
    ``cookie_secure`` outright (ADR-0003 §Demo account gate, amended 2026-08-02) — a
    sole-signal gate defaulting on would have re-opened exactly this login.
    """
    with Session(engine) as s:
        auth_service.register_user(s, email=demo.DEMO_EMAIL, password=demo.DEMO_PASSWORD)
        auth_service.register_user(s, email="real@example.com", password=_PW)

    for demo_flag in (False, True):
        hardened = Settings(
            cookie_secure=True,
            demo_login_enabled=demo_flag,
            jwt_secret="a-real-random-secret",
            cors_allowed_origins="http://localhost:3000",
        )
        assert hardened.demo_login_permitted is False
        monkeypatch.setattr(auth_service, "get_settings", lambda s=hardened: s)

        with Session(engine) as s:
            assert (
                auth_service.authenticate(s, email=demo.DEMO_EMAIL, password=demo.DEMO_PASSWORD)
                is None
            ), f"demo creds accepted on a hardened deploy with DEMO_LOGIN_ENABLED={demo_flag}"
            # A real user is unaffected by the demo gate.
            assert auth_service.authenticate(s, email="real@example.com", password=_PW) is not None


def test_authenticate_rejects_demo_login_by_default_on_plain_http(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped default refuses the demo credentials on PLAIN HTTP — the case the
    old cookie_secure-only gate left permanently open on every LAN self-host stack
    (deploy/Caddyfile serves :80, so cookie_secure is unsettable there).

    Opting in re-enables it, so the flag is a gate and not a wall.
    """
    with Session(engine) as s:
        auth_service.register_user(s, email=demo.DEMO_EMAIL, password=demo.DEMO_PASSWORD)
        auth_service.register_user(s, email="real@example.com", password=_PW)

    # tests/conftest.py's _load_dotenv() copies this box's repo-root .env
    # (DEMO_LOGIN_ENABLED=true, for the local demo seeder) into os.environ at
    # session start — an explicit env var pydantic-settings reads regardless of
    # `_env_file=None` below, which only skips re-reading the file itself.
    # Both together are what let `demo_login_enabled` genuinely resolve through
    # to the field's Python default, rather than being forced via a
    # constructor kwarg that would pass even if that default silently flipped.
    monkeypatch.delenv("DEMO_LOGIN_ENABLED", raising=False)
    default = Settings(_env_file=None, cors_allowed_origins="http://localhost:3000")
    assert default.demo_login_enabled is False, "must exercise the shipped default, not an override"
    assert default.cookie_secure is False, "this test must exercise the plain-http case"
    monkeypatch.setattr(auth_service, "get_settings", lambda: default)
    with Session(engine) as s:
        assert (
            auth_service.authenticate(s, email=demo.DEMO_EMAIL, password=demo.DEMO_PASSWORD) is None
        )
        # Not a global login kill switch — everyone else still authenticates.
        assert auth_service.authenticate(s, email="real@example.com", password=_PW) is not None

    opted_in = Settings(demo_login_enabled=True, cors_allowed_origins="http://localhost:3000")
    monkeypatch.setattr(auth_service, "get_settings", lambda: opted_in)
    with Session(engine) as s:
        assert (
            auth_service.authenticate(s, email=demo.DEMO_EMAIL, password=demo.DEMO_PASSWORD)
            is not None
        )


def test_rotate_rejects_past_absolute_cap(engine: Engine) -> None:
    """A refresh family can't be rotated past the absolute lifetime cap even while
    live/unexpired, and hitting the cap revokes the whole family (OWASP absolute
    timeout, server-enforced against the family origin).

    Drives the DATA, not the clock: the family origin is back-dated with raw SQL, so the
    row looks exactly like a genuinely old one and **no application symbol is patched**.
    The previous version fast-forwarded ``clock.naive_utcnow``, which re-implements the
    very expression it was stubbing — it would have kept passing had ``created_at`` been
    written by the DB's clock in a different timezone, which is the actual defect (B#55).

    Only ``created_at`` moves; ``expires_at`` stays in the future, so the token is live on
    the sliding TTL and the absolute cap is the sole thing rejecting it.
    """
    with Session(engine) as s:
        user = auth_service.register_user(s, email="cap@example.com", password=_PW)
        raw = auth_service.start_session(s, user.id)

    ttl = auth_service.get_settings().session_absolute_ttl_hours
    origin = clock.naive_utcnow() - timedelta(hours=ttl + 1)
    with Session(engine) as s:
        # Bound as the string SQLAlchemy's SQLite DATETIME writes ("YYYY-MM-DD HH:MM:SS.ffffff")
        # rather than a datetime object, which raw SQL would push through sqlite3's
        # adapter — deprecated since 3.12 and not the app's storage path anyway.
        s.execute(
            text("UPDATE sessions SET created_at = :t"),
            {"t": origin.isoformat(sep=" ")},
        )
        s.commit()

    with Session(engine) as s:
        assert auth_service.rotate_session(s, raw) is None
        # The whole family is revoked, not just the presented row.
        assert s.scalars(select(RefreshSession.revoked_at)).all() != []
        assert all(r is not None for r in s.scalars(select(RefreshSession.revoked_at)).all())


def test_rotate_succeeds_within_absolute_cap(engine: Engine) -> None:
    """Control: inside the window a live token still rotates normally."""
    with Session(engine) as s:
        user = auth_service.register_user(s, email="within@example.com", password=_PW)
        user_id = user.id  # capture before the session closes (attrs expire on commit)
        raw = auth_service.start_session(s, user_id)
    with Session(engine) as s:
        rotated = auth_service.rotate_session(s, raw)
        assert rotated is not None
        assert rotated.user_id == user_id


def test_verify_password_returns_false_on_malformed_hash() -> None:
    """A corrupt/non-argon2 stored hash is a failed verification (False), not a 500."""
    assert verify_password("whatever", "not-a-valid-argon2-hash") is False
