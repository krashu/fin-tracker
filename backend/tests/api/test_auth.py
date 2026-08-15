"""Auth flow tests (PRD §Users & access v2).

Covers register/login/refresh/logout/me, refresh rotation + reuse→family
revocation, access-token expiry, demo login, CSRF Origin enforcement, and the
rate limiter. Uses ``unauth_client`` (no auth cookie) so each flow starts cold.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.core import clock, security
from app.core.config import Settings, get_settings
from app.core.demo import DEMO_EMAIL, DEMO_PASSWORD
from app.core.security import ACCESS_COOKIE_NAME, REFRESH_COOKIE_NAME
from app.models import User
from app.services.provisioning import _DEFAULT_INCOME_TAXONOMY, _DEFAULT_SPEND_TAXONOMY

_EMAIL = "alice@example.com"
_PW = "correct horse battery"
_NEW_PW = "a different secret"


def _register(client: TestClient, email: str = _EMAIL, password: str = _PW) -> None:
    r = client.post("/api/v1/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text


def _demo_enabled() -> Settings:
    """Settings that permit the demo login: opted in, on plain http (the AND of
    ``Settings.demo_login_permitted``). A purpose-built instance rather than a mutation
    of the cached singleton, matching the hardened-deploy helpers below."""
    return Settings(demo_login_enabled=True, cors_allowed_origins="http://localhost:3000")


def _demo_disabled() -> Settings:
    """The shipped default: no operator opt-in.

    ``_env_file=None`` skips the ambient dotenv file, so ``demo_login_enabled``
    genuinely resolves through to the field's Python default (``False``)
    rather than being forced via a constructor kwarg (which would pass even if
    that default silently flipped to ``True``). Callers must ALSO
    ``monkeypatch.delenv("DEMO_LOGIN_ENABLED")`` — this box's repo-root
    ``.env`` sets it ``true`` for the local demo seeder, and
    ``tests/conftest.py`` copies that into real ``os.environ`` at session
    start, which pydantic-settings reads regardless of ``_env_file``.
    """
    return Settings(_env_file=None, cors_allowed_origins="http://localhost:3000")


# --- register ----------------------------------------------------------------
def test_register_creates_user_and_sets_cookies(unauth_client: TestClient) -> None:
    r = unauth_client.post("/api/v1/auth/register", json={"email": _EMAIL, "password": _PW})
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == _EMAIL
    assert "password" not in body and "password_hash" not in body
    # Cookies set, and the token is NOT in the body.
    assert ACCESS_COOKIE_NAME in unauth_client.cookies
    assert REFRESH_COOKIE_NAME in unauth_client.cookies


# A fresh registrant must get exactly the taxonomy provisioning.py defines — the name-level
# drift guard is derived from _DEFAULT_SPEND_TAXONOMY / _DEFAULT_INCOME_TAXONOMY rather than
# restated here (ADR-0012: "do not enumerate the taxonomy anywhere but provisioning.py").
# Pins the registration path end to end (auth_service.register_user ->
# provision_default_categories -> the /categories response) against silently dropping a row,
# a name, or the color-inheritance invariant (decision #5) along the way.
def test_register_provisions_default_categories(unauth_client: TestClient) -> None:
    _register(unauth_client)
    cats = unauth_client.get("/api/v1/categories").json()
    parents = [c for c in cats if c["parent_id"] is None]
    children = [c for c in cats if c["parent_id"] is not None]

    expected_parent_names = {name for name, _, _ in _DEFAULT_SPEND_TAXONOMY} | {
        name for name, _, _ in _DEFAULT_INCOME_TAXONOMY
    }
    expected_child_names = {sub for _, _, subs in _DEFAULT_SPEND_TAXONOMY for sub in subs} | {
        sub for _, _, subs in _DEFAULT_INCOME_TAXONOMY for sub in subs
    }

    assert {c["name"] for c in parents} == expected_parent_names
    assert {c["name"] for c in children} == expected_child_names
    assert len(parents) == 10  # 9 spend parents + 1 income parent
    assert len(children) == len(expected_child_names)
    assert all(c["is_seeded"] for c in cats)  # app defaults, not user-created
    assert all(c["color"] for c in parents)  # parent colors provisioned, none null
    assert all(c["color"] is None for c in children)  # seeded children inherit (decision #5)


def test_register_duplicate_email_409(unauth_client: TestClient) -> None:
    _register(unauth_client)
    r = unauth_client.post(
        "/api/v1/auth/register", json={"email": _EMAIL, "password": "another one!"}
    )
    assert r.status_code == 409


def test_register_email_is_case_insensitive_for_dupes(unauth_client: TestClient) -> None:
    _register(unauth_client, email="Bob@Example.com")
    r = unauth_client.post(
        "/api/v1/auth/register", json={"email": "bob@example.com", "password": "xxxxxxxx"}
    )
    assert r.status_code == 409


def test_register_rejects_short_password(unauth_client: TestClient) -> None:
    r = unauth_client.post("/api/v1/auth/register", json={"email": _EMAIL, "password": "short"})
    assert r.status_code == 422


# --- login -------------------------------------------------------------------
def test_login_success(unauth_client: TestClient) -> None:
    _register(unauth_client)
    unauth_client.cookies.clear()
    r = unauth_client.post("/api/v1/auth/login", json={"email": _EMAIL, "password": _PW})
    assert r.status_code == 200
    assert ACCESS_COOKIE_NAME in unauth_client.cookies


def test_login_wrong_password_401(unauth_client: TestClient) -> None:
    _register(unauth_client)
    r = unauth_client.post(
        "/api/v1/auth/login", json={"email": _EMAIL, "password": "wrong password"}
    )
    assert r.status_code == 401


def test_login_unknown_email_401(unauth_client: TestClient) -> None:
    r = unauth_client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": _PW}
    )
    assert r.status_code == 401


def test_demo_login_works_when_opted_in(
    unauth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seeded user carries the demo creds — the 'Try the demo' path, which needs
    the operator's explicit opt-in (the gate is closed by default; see below)."""
    monkeypatch.setattr("app.services.auth_service.get_settings", _demo_enabled)
    r = unauth_client.post(
        "/api/v1/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
    )
    assert r.status_code == 200


def test_demo_login_refused_by_default_and_config_agrees(
    unauth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B9.1: on PLAIN HTTP with DEMO_LOGIN_ENABLED unset, the demo creds must 401 AND
    GET /auth/config must advertise the same answer.
    """
    # tests/conftest.py's _load_dotenv() copies this box's repo-root .env
    # (DEMO_LOGIN_ENABLED=true, for the local demo seeder) into os.environ at
    # session start — an explicit env var pydantic-settings reads regardless
    # of _demo_disabled's `_env_file=None`. Unset it so this test genuinely
    # exercises the shipped default, not whatever dev convenience is ambient.
    monkeypatch.delenv("DEMO_LOGIN_ENABLED", raising=False)
    monkeypatch.setattr("app.api.v1.auth.get_settings", _demo_disabled)
    monkeypatch.setattr("app.services.auth_service.get_settings", _demo_disabled)
    assert get_settings().cookie_secure is False, "must exercise the plain-http case"

    r = unauth_client.post(
        "/api/v1/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
    )
    assert r.status_code == 401
    assert unauth_client.get("/api/v1/auth/config").json()["demo_login_enabled"] is False


def test_gated_demo_does_not_block_a_real_user(unauth_client: TestClient) -> None:
    """The demo gate is not a global login kill switch — everyone else still logs in
    with it closed."""
    _register(unauth_client)
    unauth_client.cookies.clear()
    r = unauth_client.post("/api/v1/auth/login", json={"email": _EMAIL, "password": _PW})
    assert r.status_code == 200


# --- me ----------------------------------------------------------------------
def test_me_requires_auth(unauth_client: TestClient) -> None:
    assert unauth_client.get("/api/v1/auth/me").status_code == 401


def test_me_returns_current_user(unauth_client: TestClient) -> None:
    _register(unauth_client)
    r = unauth_client.get("/api/v1/auth/me")
    assert r.status_code == 200
    assert r.json()["email"] == _EMAIL


def test_expired_access_token_401(
    unauth_client: TestClient, seeded_user: User, monkeypatch
) -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    monkeypatch.setattr(clock, "utcnow", lambda: past)
    token = security.create_access_token(seeded_user.id)  # exp = 45 min ago
    monkeypatch.undo()  # decode uses real clock → token is expired
    unauth_client.cookies.set(ACCESS_COOKIE_NAME, token)
    assert unauth_client.get("/api/v1/auth/me").status_code == 401


# --- refresh rotation + reuse -----------------------------------------------
def test_refresh_rotates_token(unauth_client: TestClient) -> None:
    _register(unauth_client)
    old_refresh = unauth_client.cookies[REFRESH_COOKIE_NAME]
    r = unauth_client.post("/api/v1/auth/refresh")
    assert r.status_code == 200
    new_refresh = unauth_client.cookies[REFRESH_COOKIE_NAME]
    assert new_refresh != old_refresh  # rotated


def _set_only_refresh(client: TestClient, token: str) -> None:
    """Replace the cookie jar with a single refresh cookie (avoids a duplicate
    name at a different path when we hand-set a token the server issued at
    /api/v1/auth)."""
    client.cookies.clear()
    client.cookies.set(REFRESH_COOKIE_NAME, token)


def test_refresh_reuse_revokes_family(unauth_client: TestClient) -> None:
    _register(unauth_client)
    stolen = unauth_client.cookies[REFRESH_COOKIE_NAME]
    # Legit rotation → the stolen (now-revoked) token is reused below.
    assert unauth_client.post("/api/v1/auth/refresh").status_code == 200
    rotated = unauth_client.cookies[REFRESH_COOKIE_NAME]

    # Reuse the old, already-rotated token → 401 AND revokes the whole family.
    _set_only_refresh(unauth_client, stolen)
    assert unauth_client.post("/api/v1/auth/refresh").status_code == 401
    # The legitimately-rotated successor is now dead too (family revoked).
    _set_only_refresh(unauth_client, rotated)
    assert unauth_client.post("/api/v1/auth/refresh").status_code == 401


def test_refresh_without_cookie_401(unauth_client: TestClient) -> None:
    assert unauth_client.post("/api/v1/auth/refresh").status_code == 401


def test_refresh_unknown_token_401(unauth_client: TestClient) -> None:
    _set_only_refresh(unauth_client, "not-a-real-token")
    assert unauth_client.post("/api/v1/auth/refresh").status_code == 401


def test_logout_revokes_refresh(unauth_client: TestClient) -> None:
    _register(unauth_client)
    refresh = unauth_client.cookies[REFRESH_COOKIE_NAME]
    assert unauth_client.post("/api/v1/auth/logout").status_code == 204
    # The revoked refresh token no longer rotates.
    _set_only_refresh(unauth_client, refresh)
    assert unauth_client.post("/api/v1/auth/refresh").status_code == 401


# --- CSRF Origin enforcement -------------------------------------------------
def test_csrf_foreign_origin_rejected(unauth_client: TestClient) -> None:
    r = unauth_client.post(
        "/api/v1/auth/login",
        json={"email": _EMAIL, "password": _PW},
        headers={"origin": "http://evil.example.com"},
    )
    assert r.status_code == 403


def test_csrf_missing_origin_rejected(unauth_client: TestClient) -> None:
    del unauth_client.headers["origin"]  # drop the fixture default
    r = unauth_client.post("/api/v1/auth/login", json={"email": _EMAIL, "password": _PW})
    assert r.status_code == 403


def test_csrf_allows_safe_get_without_origin(unauth_client: TestClient) -> None:
    del unauth_client.headers["origin"]
    assert unauth_client.get("/api/v1/health").status_code == 200


# --- rate limiting -----------------------------------------------------------
def test_login_rate_limited(unauth_client: TestClient, monkeypatch) -> None:
    """After the per-minute budget (default 20), the next login is 429.

    Freeze the limiter's clock so the burst can't straddle a fixed-window
    boundary (which would reset the counter and flake the test)."""
    monkeypatch.setattr("app.core.rate_limit.time.time", lambda: 1_000_000.0)
    body = {"email": "nobody@example.com", "password": "whatever!"}
    for _ in range(20):
        assert unauth_client.post("/api/v1/auth/login", json=body).status_code == 401
    assert unauth_client.post("/api/v1/auth/login", json=body).status_code == 429


# --- refresh cookie scoping --------------------------------------------------
def test_refresh_cookie_scoped_to_auth_path(unauth_client: TestClient) -> None:
    """The refresh cookie is path-scoped to the auth router, derived from the
    single mount-prefix source of truth (guards the main.py↔auth.py coupling)."""
    r = unauth_client.post("/api/v1/auth/register", json={"email": _EMAIL, "password": _PW})
    assert r.status_code == 201
    refresh_headers = [
        h for h in r.headers.get_list("set-cookie") if h.startswith(f"{REFRESH_COOKIE_NAME}=")
    ]
    assert refresh_headers, r.headers.get_list("set-cookie")
    assert "Path=/api/v1/auth" in refresh_headers[0]


# --- change-password ---------------------------------------------------------
def test_change_password_success(unauth_client: TestClient) -> None:
    _register(unauth_client)
    r = unauth_client.post(
        "/api/v1/auth/change-password",
        json={"current_password": _PW, "new_password": _NEW_PW},
    )
    assert r.status_code == 200, r.text
    # Fresh cookies set for the acting device, and the caller stays authenticated.
    assert ACCESS_COOKIE_NAME in unauth_client.cookies
    assert REFRESH_COOKIE_NAME in unauth_client.cookies
    assert unauth_client.get("/api/v1/auth/me").status_code == 200


def test_change_password_wrong_current_400(unauth_client: TestClient) -> None:
    _register(unauth_client)
    r = unauth_client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "wrong password", "new_password": _NEW_PW},
    )
    assert r.status_code == 400


def test_change_password_same_as_current_400(unauth_client: TestClient) -> None:
    _register(unauth_client)
    r = unauth_client.post(
        "/api/v1/auth/change-password",
        json={"current_password": _PW, "new_password": _PW},
    )
    assert r.status_code == 400


def test_change_password_requires_auth(unauth_client: TestClient) -> None:
    # No access cookie → CurrentUser 401 before any password logic runs.
    r = unauth_client.post(
        "/api/v1/auth/change-password",
        json={"current_password": _PW, "new_password": _NEW_PW},
    )
    assert r.status_code == 401


def test_change_password_rejects_short_new_password(unauth_client: TestClient) -> None:
    _register(unauth_client)
    r = unauth_client.post(
        "/api/v1/auth/change-password",
        json={"current_password": _PW, "new_password": "short"},
    )
    assert r.status_code == 422


def test_change_password_new_login_works_old_fails(unauth_client: TestClient) -> None:
    _register(unauth_client)
    assert (
        unauth_client.post(
            "/api/v1/auth/change-password",
            json={"current_password": _PW, "new_password": _NEW_PW},
        ).status_code
        == 200
    )
    unauth_client.cookies.clear()
    assert (
        unauth_client.post(
            "/api/v1/auth/login", json={"email": _EMAIL, "password": _PW}
        ).status_code
        == 401
    )
    assert (
        unauth_client.post(
            "/api/v1/auth/login", json={"email": _EMAIL, "password": _NEW_PW}
        ).status_code
        == 200
    )


def test_change_password_signs_out_other_sessions_keeps_current(unauth_client: TestClient) -> None:
    """Every other refresh family dies; the acting device continues on fresh cookies."""
    _register(unauth_client)  # session A (family F1), cookies in the jar
    a_refresh = unauth_client.cookies[REFRESH_COOKIE_NAME]
    # A second device logs in → session B (family F2); the jar now holds B's cookies.
    assert (
        unauth_client.post(
            "/api/v1/auth/login", json={"email": _EMAIL, "password": _PW}
        ).status_code
        == 200
    )
    b_refresh = unauth_client.cookies[REFRESH_COOKIE_NAME]
    assert b_refresh != a_refresh

    # Acting device (= session B, the current jar) changes the password.
    assert (
        unauth_client.post(
            "/api/v1/auth/change-password",
            json={"current_password": _PW, "new_password": _NEW_PW},
        ).status_code
        == 200
    )
    current_refresh = unauth_client.cookies[REFRESH_COOKIE_NAME]
    assert current_refresh not in (a_refresh, b_refresh)  # a brand-new family
    # Acting device survives: its fresh access cookie authenticates.
    assert unauth_client.get("/api/v1/auth/me").status_code == 200

    # Both prior sessions are dead (their refresh tokens no longer rotate)...
    _set_only_refresh(unauth_client, a_refresh)
    assert unauth_client.post("/api/v1/auth/refresh").status_code == 401
    _set_only_refresh(unauth_client, b_refresh)
    assert unauth_client.post("/api/v1/auth/refresh").status_code == 401
    # ...while the acting device's fresh refresh token still rotates.
    _set_only_refresh(unauth_client, current_refresh)
    assert unauth_client.post("/api/v1/auth/refresh").status_code == 200


def test_access_token_survives_password_change_until_expiry(unauth_client: TestClient) -> None:
    """Documents the deliberate ~15-min lag: revoking refresh families does NOT
    invalidate already-issued access JWTs (they're decoded statelessly, without a
    revocation check). An 'other device' holding a still-valid access cookie keeps
    working until that cookie expires — this is expected, not a bug to 'fix'."""
    r = unauth_client.post("/api/v1/auth/register", json={"email": _EMAIL, "password": _PW})
    assert r.status_code == 201
    user_id = UUID(r.json()["id"])
    other_access = security.create_access_token(user_id)  # a still-valid access token

    assert (
        unauth_client.post(
            "/api/v1/auth/change-password",
            json={"current_password": _PW, "new_password": _NEW_PW},
        ).status_code
        == 200
    )

    # Swap the jar to hold only the other device's pre-change access cookie.
    unauth_client.cookies.clear()
    unauth_client.cookies.set(ACCESS_COOKIE_NAME, other_access)
    assert unauth_client.get("/api/v1/auth/me").status_code == 200


def test_change_password_refused_for_demo_account(client: TestClient) -> None:
    """The shared, source-published demo creds must never be rotated by a demo
    session — that would break 'Try the demo' for the next visitor. (`client` is
    authenticated as the seeded demo user.)"""
    r = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": DEMO_PASSWORD, "new_password": _NEW_PW},
    )
    assert r.status_code == 403


def test_change_password_csrf_foreign_origin_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": DEMO_PASSWORD, "new_password": _NEW_PW},
        headers={"origin": "http://evil.example.com"},
    )
    assert r.status_code == 403


# --- auth config (demo-gate signal) ------------------------------------------
def test_auth_config_demo_disabled_by_default(
    unauth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped default is CLOSED, on plain http as much as anywhere else — so the
    login page hides 'Try the demo' until an operator opts in."""
    # See test_demo_login_refused_by_default_and_config_agrees — this box's .env
    # leaks DEMO_LOGIN_ENABLED=true into os.environ via tests/conftest.py.
    monkeypatch.delenv("DEMO_LOGIN_ENABLED", raising=False)
    monkeypatch.setattr("app.api.v1.auth.get_settings", _demo_disabled)
    r = unauth_client.get("/api/v1/auth/config")
    assert r.status_code == 200
    assert r.json()["demo_login_enabled"] is False
    assert r.json()["registration_enabled"] is True


def test_auth_config_demo_enabled_when_opted_in(
    unauth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.v1.auth.get_settings", _demo_enabled)
    body = unauth_client.get("/api/v1/auth/config").json()
    assert body["demo_login_enabled"] is True
    assert body["registration_enabled"] is True


def test_auth_config_registration_disabled(
    unauth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_reg = Settings(
        registration_enabled=False,
        demo_login_enabled=True,
        cors_allowed_origins="http://localhost:3000",
    )
    monkeypatch.setattr("app.api.v1.auth.get_settings", lambda: no_reg)
    body = unauth_client.get("/api/v1/auth/config").json()
    assert body["registration_enabled"] is False
    assert body["demo_login_enabled"] is True


def test_register_refused_when_registration_disabled(
    unauth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    no_reg = Settings(
        registration_enabled=False,
        cors_allowed_origins="http://localhost:3000",
    )
    monkeypatch.setattr("app.api.v1.auth.get_settings", lambda: no_reg)
    r = unauth_client.post("/api/v1/auth/register", json={"email": _EMAIL, "password": _PW})
    assert r.status_code == 403
    assert r.json()["detail"] == "registration is disabled"


@pytest.mark.parametrize("demo_flag", [False, True])
def test_auth_config_demo_disabled_when_cookie_secure(
    unauth_client: TestClient, monkeypatch: pytest.MonkeyPatch, demo_flag: bool
) -> None:
    """On a hardened deploy without explicit override, the login page must hide 'Try the demo'."""
    hardened = Settings(
        cookie_secure=True,
        demo_login_enabled=demo_flag,
        allow_demo_login_over_https=False,
        jwt_secret="a-real-random-secret",
        cors_allowed_origins="http://localhost:3000",
    )
    monkeypatch.setattr("app.api.v1.auth.get_settings", lambda: hardened)
    r = unauth_client.get("/api/v1/auth/config")
    assert r.status_code == 200
    assert r.json()["demo_login_enabled"] is False


def test_demo_login_allowed_over_https_when_explicitly_permitted(
    unauth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dedicated cloud demo showcase can allow demo login over HTTPS."""
    demo_cloud = Settings(
        cookie_secure=True,
        demo_login_enabled=True,
        allow_demo_login_over_https=True,
        jwt_secret="a-real-random-secret",
        cors_allowed_origins="http://localhost:3000",
    )
    monkeypatch.setattr("app.api.v1.auth.get_settings", lambda: demo_cloud)
    monkeypatch.setattr("app.services.auth_service.get_settings", lambda: demo_cloud)

    r = unauth_client.get("/api/v1/auth/config")
    assert r.status_code == 200
    assert r.json()["demo_login_enabled"] is True

    # And authenticate actually succeeds with the demo credentials
    r_login = unauth_client.post(
        "/api/v1/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
    )
    assert r_login.status_code == 200


def test_demo_login_refused_when_demo_login_disabled_even_if_https_override_set() -> None:
    """If DEMO_LOGIN_ENABLED is False, demo_login_permitted must stay False regardless of
    allow_demo_login_over_https."""
    s = Settings(
        demo_login_enabled=False,
        allow_demo_login_over_https=True,
        cookie_secure=True,
        jwt_secret="a-real-random-secret",
        cors_allowed_origins="http://localhost:3000",
    )
    assert s.demo_login_permitted is False


def test_validate_cookie_policy_requires_secure_for_samesite_none() -> None:
    """COOKIE_SAMESITE=none must raise ValidationError if COOKIE_SECURE is False."""
    with pytest.raises(Exception, match="COOKIE_SAMESITE=none requires COOKIE_SECURE=true"):
        Settings(
            cookie_samesite="none",
            cookie_secure=False,
            cors_allowed_origins="http://localhost:3000",
        )
