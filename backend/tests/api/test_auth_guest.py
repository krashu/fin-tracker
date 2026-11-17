"""Tests for ephemeral guest demo session auth flow."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import clock
from app.core.config import Settings
from app.core.demo_data import ACCOUNTS
from app.core.security import ACCESS_COOKIE_NAME, REFRESH_COOKIE_NAME, create_access_token
from app.models import Account, ImportBatch, User
from app.services import guest_service


def _guests_enabled() -> Settings:
    return Settings(guest_sandbox_enabled=True, cors_allowed_origins="http://localhost:3000")


def test_demo_session_creates_guest_and_sets_cookies(
    unauth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.v1.auth.get_settings", _guests_enabled)
    monkeypatch.setattr("app.core.config.get_settings", _guests_enabled)

    r = unauth_client.post("/api/v1/auth/demo-session")
    assert r.status_code == 201, r.text
    body = r.json()

    assert body["is_guest"] is True
    assert body["can_manage_credentials"] is False
    assert body["display_name"] == "Demo Guest"
    assert body["email"] is None
    assert ACCESS_COOKIE_NAME in unauth_client.cookies
    assert REFRESH_COOKIE_NAME in unauth_client.cookies

    # Verify guest can access protected routes and has seeded accounts & transactions
    accounts_res = unauth_client.get("/api/v1/accounts")
    assert accounts_res.status_code == 200
    assert len(accounts_res.json()) == len(ACCOUNTS)

    txns_res = unauth_client.get("/api/v1/transactions")
    assert txns_res.status_code == 200
    assert len(txns_res.json()) > 0


def test_demo_session_refused_when_guest_sandbox_disabled(
    unauth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0003 rule 15: guests have their own gate. The shared demo login being
    permitted (opted in, plain http) does not open it."""
    monkeypatch.delenv("GUEST_SANDBOX_ENABLED", raising=False)
    demo_only = Settings(
        _env_file=None, demo_login_enabled=True, cors_allowed_origins="http://localhost:3000"
    )
    assert demo_only.demo_login_permitted is True
    assert demo_only.guest_sandbox_enabled is False, "must exercise the shipped default"
    monkeypatch.setattr("app.api.v1.auth.get_settings", lambda: demo_only)

    r = unauth_client.post("/api/v1/auth/demo-session")
    assert r.status_code == 403
    assert r.json()["detail"] == "guest sandboxes are disabled on this instance"


def test_guest_sandbox_runs_over_https(
    unauth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0003 rule 15: unlike the shared demo login, a guest sandbox may run over
    HTTPS. TestClient talks plain http and won't send Secure cookies back, so only the
    201 body is asserted."""
    hosted = Settings(
        cookie_secure=True,
        guest_sandbox_enabled=True,
        jwt_secret="a-real-random-secret",
        cors_allowed_origins="http://localhost:3000",
    )
    monkeypatch.setattr("app.api.v1.auth.get_settings", lambda: hosted)

    r = unauth_client.post("/api/v1/auth/demo-session")
    assert r.status_code == 201, r.text
    assert r.json()["is_guest"] is True


def test_multiple_guest_sessions_are_isolated(
    unauth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.v1.auth.get_settings", _guests_enabled)
    monkeypatch.setattr("app.core.config.get_settings", _guests_enabled)

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


def test_demo_session_returns_503_on_capacity(
    unauth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _demo_capped() -> Settings:
        return Settings(
            guest_sandbox_enabled=True,
            cors_allowed_origins="http://localhost:3000",
            MAX_GUEST_ACCOUNTS=1,
        )

    monkeypatch.setattr("app.api.v1.auth.get_settings", _demo_capped)
    monkeypatch.setattr("app.core.config.get_settings", _demo_capped)
    monkeypatch.setattr("app.services.guest_service.get_settings", _demo_capped)

    # 1st succeeds
    r1 = unauth_client.post("/api/v1/auth/demo-session")
    assert r1.status_code == 201

    # 2nd reaches cap -> 503
    unauth_client.cookies.clear()
    r2 = unauth_client.post("/api/v1/auth/demo-session")
    assert r2.status_code == 503
    assert "demo is at capacity" in r2.json()["detail"]


def test_pruned_guest_token_is_401(
    unauth_client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0003 rule 4: a still-valid access token for a user that no longer exists is
    401 on every route, never 200 over an empty tenant nor a 500 from a later FK
    failure on write."""
    monkeypatch.setattr("app.api.v1.auth.get_settings", _guests_enabled)
    r = unauth_client.post("/api/v1/auth/demo-session")
    assert r.status_code == 201, r.text
    guest_service.delete_guest_sandbox(session, UUID(r.json()["id"]))

    assert unauth_client.get("/api/v1/accounts").status_code == 401
    created = unauth_client.post(
        "/api/v1/accounts", json={"name": "Ghost", "type": "bank", "issuer": "hdfc"}
    )
    assert created.status_code == 401


def test_expired_guest_token_is_401(
    unauth_client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guest past ``guest_expires_at`` but not yet pruned by the sweeper is 401 on
    its still-valid access token, matching what refresh already does."""
    monkeypatch.setattr("app.api.v1.auth.get_settings", _guests_enabled)
    r = unauth_client.post("/api/v1/auth/demo-session")
    assert r.status_code == 201, r.text
    guest = session.get(User, UUID(r.json()["id"]))
    assert guest is not None
    guest.guest_expires_at = clock.naive_utcnow() - timedelta(minutes=1)
    session.commit()

    assert unauth_client.get("/api/v1/accounts").status_code == 401


@pytest.mark.parametrize("route", ["statement", "investment_csv", "backup"])
def test_guest_upload_is_refused(unauth_client: TestClient, session: Session, route: str) -> None:
    """ADR-0003 rule 15: a guest cannot upload files — every upload route is 403 and
    stages nothing. (Guests see sample data only; never a real statement.)"""
    guest, _ = guest_service.create_guest_sandbox(session)
    account_id = session.scalars(select(Account.id).where(Account.user_id == guest.id)).first()
    assert account_id is not None
    unauth_client.cookies.set(ACCESS_COOKIE_NAME, create_access_token(guest.id))

    if route == "statement":
        r = unauth_client.post(
            "/api/v1/imports",
            data={"account_id": str(account_id)},
            files={"file": ("statement.pdf", b"%PDF-1.4 dummy", "application/pdf")},
        )
    elif route == "investment_csv":
        r = unauth_client.post(
            "/api/v1/imports/investments",
            data={"asset_class": "indian_mf"},
            files={"file": ("tradebook.csv", b"date,symbol\n", "text/csv")},
        )
    else:
        r = unauth_client.post(
            "/api/v1/backup/import",
            files={"file": ("backup.zip", b"not a zip", "application/zip")},
        )

    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "guests cannot upload files; use a sample statement"
    staged = session.scalars(select(ImportBatch.id).where(ImportBatch.user_id == guest.id))
    assert staged.all() == []


def test_guest_can_import_a_sample_statement(unauth_client: TestClient, session: Session) -> None:
    """ADR-0003 rule 15: a guest cannot upload, but can import a bundled sample into the
    matching demo account, and the staged rows are exactly the sample's merchants."""
    guest, _ = guest_service.create_guest_sandbox(session)
    card_id = session.scalars(
        select(Account.id).where(
            Account.user_id == guest.id, Account.type == "credit_card", Account.issuer == "axis"
        )
    ).one()
    unauth_client.cookies.set(ACCESS_COOKIE_NAME, create_access_token(guest.id))

    listed = unauth_client.get("/api/v1/imports/samples")
    assert listed.status_code == 200, listed.text
    [sample] = [s for s in listed.json() if s["id"] == "axis-cc-2025-06"]
    assert sample["account_ids"] == [card_id]

    r = unauth_client.post(
        "/api/v1/imports/samples", json={"sample_id": "axis-cc-2025-06", "account_id": card_id}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == body["pending_count"] == 12

    batch = session.get(ImportBatch, body["batch_id"])
    assert batch is not None and batch.user_id == guest.id
    candidates = unauth_client.get(f"/api/v1/imports/{body['batch_id']}/candidates").json()
    assert sorted(c["merchant_raw"] for c in candidates) == [
        "BIGBASKET BANGALORE",
        "CASHBACK CREDIT",
        "DOMINOS PIZZA",
        "NETFLIX SUBSCRIPTION",
        "PAYMENT RECEIVED THANK YOU",
        "REFUND UBER INDIA",
        "STARBUCKS BANGALORE",
        "SWIGGY BANGALORE",
        "SWIGGY BANGALORE",
        "UBER INDIA",
        "UBER INDIA",
        "ZOMATO ONLINE",
    ]


def test_guest_can_import_the_hdfc_sample(unauth_client: TestClient, session: Session) -> None:
    """ADR-0003 rule 15: the guest's HDFC savings account is offered the bank sample, the
    import stages the sample's merchants, and its card-bill autopay debit is flagged for
    the F4a-1 link."""
    guest, _ = guest_service.create_guest_sandbox(session)
    bank_id = session.scalars(
        select(Account.id).where(
            Account.user_id == guest.id, Account.type == "bank", Account.issuer == "hdfc"
        )
    ).one()
    unauth_client.cookies.set(ACCESS_COOKIE_NAME, create_access_token(guest.id))

    listed = unauth_client.get("/api/v1/imports/samples")
    assert listed.status_code == 200, listed.text
    [sample] = [s for s in listed.json() if s["id"] == "hdfc-savings-2025-06"]
    assert sample["account_ids"] == [bank_id]

    r = unauth_client.post(
        "/api/v1/imports/samples", json={"sample_id": "hdfc-savings-2025-06", "account_id": bank_id}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == body["pending_count"] == 12

    candidates = unauth_client.get(f"/api/v1/imports/{body['batch_id']}/candidates").json()
    merchants = [c["merchant_raw"] for c in candidates]
    for brand in ("SWIGGY", "UBER INDIA", "BIGBASKET", "NETFLIX", "ZOMATO", "STARBUCKS", "DOMINOS"):
        assert any(brand in m for m in merchants), brand
    flagged = [c["merchant_raw"] for c in candidates if c["cc_payment_candidate"]]
    assert flagged == ["CC 000000XXXXXX0000 AUTOPAY SI-TAD"]
