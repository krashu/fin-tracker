"""Tests for ephemeral guest demo session auth flow."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.security import ACCESS_COOKIE_NAME, REFRESH_COOKIE_NAME


def _demo_enabled() -> Settings:
    return Settings(demo_login_enabled=True, cors_allowed_origins="http://localhost:3000")


def _demo_disabled() -> Settings:
    return Settings(_env_file=None, cors_allowed_origins="http://localhost:3000")


def test_demo_session_creates_guest_and_sets_cookies(
    unauth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.v1.auth.get_settings", _demo_enabled)
    monkeypatch.setattr("app.core.config.get_settings", _demo_enabled)

    r = unauth_client.post("/api/v1/auth/demo-session")
    assert r.status_code == 201, r.text
    body = r.json()

    assert body["is_guest"] is True
    assert body["display_name"] == "Demo Guest"
    assert body["email"] is None
    assert ACCESS_COOKIE_NAME in unauth_client.cookies
    assert REFRESH_COOKIE_NAME in unauth_client.cookies

    # Verify guest can access protected routes and has seeded accounts & transactions
    accounts_res = unauth_client.get("/api/v1/accounts")
    assert accounts_res.status_code == 200
    assert len(accounts_res.json()) == 2

    txns_res = unauth_client.get("/api/v1/transactions")
    assert txns_res.status_code == 200
    assert len(txns_res.json()) > 0


def test_demo_session_refused_when_demo_disabled(
    unauth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DEMO_LOGIN_ENABLED", raising=False)
    monkeypatch.setattr("app.api.v1.auth.get_settings", _demo_disabled)
    monkeypatch.setattr("app.core.config.get_settings", _demo_disabled)

    r = unauth_client.post("/api/v1/auth/demo-session")
    assert r.status_code == 403
    assert "demo access is disabled" in r.json()["detail"]


def test_multiple_guest_sessions_are_isolated(
    unauth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.v1.auth.get_settings", _demo_enabled)
    monkeypatch.setattr("app.core.config.get_settings", _demo_enabled)

    # Guest 1
    r1 = unauth_client.post("/api/v1/auth/demo-session")
    assert r1.status_code == 201
    guest1_id = r1.json()["id"]

    # Guest 1 creates a unique transaction
    acc1 = unauth_client.get("/api/v1/accounts").json()[0]["id"]
    unauth_client.post(
        "/api/v1/transactions",
        json={
            "date": "2026-08-01",
            "account_id": acc1,
            "amount_paise": -99900,
            "transaction_type": "spend",
            "merchant_raw": "Guest 1 Unique Store",
        },
    )

    # Guest 2
    unauth_client.cookies.clear()
    r2 = unauth_client.post("/api/v1/auth/demo-session")
    assert r2.status_code == 201
    guest2_id = r2.json()["id"]
    assert guest1_id != guest2_id

    # Guest 2 must not see Guest 1's custom transaction
    guest2_txns = unauth_client.get("/api/v1/transactions").json()
    assert not any(t.get("merchant_raw") == "Guest 1 Unique Store" for t in guest2_txns)
