"""End-to-end tests for Net Worth endpoints & cross-user tenant isolation (PRD §F8 View 4)."""

from __future__ import annotations

import uuid
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core import clock
from app.models import (
    Account,
    Category,
    PeerContact,
    PeerEntry,
    Transaction,
    User,
)


def _make_bank(session: Session, user: User, opening_balance_paise: int = 100_000) -> Account:
    acct = Account(
        user_id=user.id,
        name="Axis Savings",
        type="bank",
        opening_balance_paise=opening_balance_paise,
    )
    session.add(acct)
    session.flush()
    return acct


def _make_txn(
    session: Session,
    user: User,
    account: Account,
    amount_paise: int,
    txn_date: date,
    txn_type: str = "spend",
    category_id: int | None = None,
) -> Transaction:
    txn = Transaction(
        user_id=user.id,
        account_id=account.id,
        date=txn_date,
        amount_paise=amount_paise,
        transaction_type=txn_type,
        category_id=category_id,
        merchant_raw="Supermart",
        merchant_normalized="supermart",
        fingerprint=uuid.uuid4().hex,
        source="manual",
        confirmed_at=clock.naive_utcnow(),
    )
    session.add(txn)
    session.flush()
    return txn


def test_net_worth_summary_endpoint(
    client: TestClient, session: Session, seeded_user: User
) -> None:
    """GET /api/v1/net-worth/summary returns headline net worth and category breakdown."""
    bank = _make_bank(session, seeded_user, opening_balance_paise=500_000)
    _make_txn(session, seeded_user, bank, -50_000, date(2026, 4, 1))

    resp = client.get("/api/v1/net-worth/summary")
    assert resp.status_code == 200
    data = resp.json()

    assert data["net_worth_paise"] == 450_000
    assert data["assets_paise"] == 450_000
    assert data["liabilities_paise"] == 0
    assert data["breakdown"]["bank_cash_paise"] == 450_000


def test_net_worth_timeline_endpoint_ranges(
    client: TestClient, session: Session, seeded_user: User
) -> None:
    """GET /api/v1/net-worth/timeline supports 1M, 6M, 1Y, 3Y, 5Y, ALL."""
    bank = _make_bank(session, seeded_user, opening_balance_paise=300_000)
    _make_txn(session, seeded_user, bank, 100_000, date(2026, 1, 10), txn_type="income")

    # 1. 1M (daily)
    resp_1m = client.get("/api/v1/net-worth/timeline?range=1M")
    assert resp_1m.status_code == 200
    data_1m = resp_1m.json()
    assert data_1m["range"] == "1M"
    assert data_1m["interval"] == "daily"
    assert len(data_1m["points"]) >= 30

    # 2. 3Y (weekly)
    resp_3y = client.get("/api/v1/net-worth/timeline?range=3Y")
    assert resp_3y.status_code == 200
    data_3y = resp_3y.json()
    assert data_3y["range"] == "3Y"
    assert data_3y["interval"] == "weekly"


def test_cashflow_waterfall_endpoint(
    client: TestClient, session: Session, seeded_user: User
) -> None:
    """GET /api/v1/dashboards/cashflow-waterfall returns annual inflow-to-wealth data."""
    bank = _make_bank(session, seeded_user, opening_balance_paise=0)
    cat_inc = Category(user_id=seeded_user.id, name="Salary", kind="income", color="#10b981")
    cat_exp = Category(user_id=seeded_user.id, name="Groceries", kind="spend", color="#f43f5e")
    session.add_all([cat_inc, cat_exp])
    session.flush()

    _make_txn(
        session,
        seeded_user,
        bank,
        2_000_000,
        date(2026, 2, 1),
        txn_type="income",
        category_id=cat_inc.id,
    )
    _make_txn(
        session,
        seeded_user,
        bank,
        -500_000,
        date(2026, 2, 15),
        txn_type="spend",
        category_id=cat_exp.id,
    )

    # Dashboard cashflow waterfall
    resp = client.get("/api/v1/dashboards/cashflow-waterfall?year=2026")
    assert resp.status_code == 200
    data = resp.json()
    assert data["year"] == 2026
    assert data["gross_income_paise"] == 2_000_000
    assert data["total_living_expenses_paise"] == -500_000
    assert data["net_liquid_surplus_paise"] == 1_500_000
    assert len(data["steps"]) >= 3


def test_net_worth_tenant_isolation(
    client: TestClient, session: Session, seeded_user: User
) -> None:
    """User A data never leaks into User B net worth summary, timeline, or waterfall (ADR-0003)."""
    # 1. User A has 5,00,000 paise in bank
    bank_a = _make_bank(session, seeded_user, opening_balance_paise=500_000)
    _make_txn(session, seeded_user, bank_a, 200_000, date(2026, 3, 1), txn_type="income")

    # Add peer entry for User A
    contact_a = PeerContact(user_id=seeded_user.id, name="Friend A")
    session.add(contact_a)
    session.flush()
    peer_a = PeerEntry(
        user_id=seeded_user.id,
        contact_id=contact_a.id,
        date=date(2026, 3, 2),
        amount_paise=100_000,
        direction="you_gave",
    )
    session.add(peer_a)
    session.flush()

    # User A summary
    resp_a = client.get("/api/v1/net-worth/summary")
    assert resp_a.status_code == 200
    assert resp_a.json()["net_worth_paise"] == 800_000

    # 2. Authenticate as User B via registration
    reg_resp = client.post(
        "/api/v1/auth/register",
        json={"email": "user.b@example.com", "password": "passphrase-123"},
    )
    assert reg_resp.status_code == 201

    # User B summary must be completely empty (0 net worth, 0 assets, 0 liabilities)
    resp_b_summary = client.get("/api/v1/net-worth/summary")
    assert resp_b_summary.status_code == 200
    data_b = resp_b_summary.json()
    assert data_b["net_worth_paise"] == 0
    assert data_b["assets_paise"] == 0
    assert data_b["liabilities_paise"] == 0
    assert data_b["breakdown"]["bank_cash_paise"] == 0
    assert data_b["breakdown"]["peer_receivables_paise"] == 0

    # User B timeline must not contain User A points
    resp_b_timeline = client.get("/api/v1/net-worth/timeline?range=1M")
    assert resp_b_timeline.status_code == 200
    for pt in resp_b_timeline.json()["points"]:
        assert pt["net_worth_paise"] == 0
        assert pt["bank_cash_paise"] == 0

    # User B cashflow waterfall must not contain User A income or expenses
    resp_b_waterfall = client.get("/api/v1/dashboards/cashflow-waterfall?year=2026")
    assert resp_b_waterfall.status_code == 200
    assert resp_b_waterfall.json()["gross_income_paise"] == 0
    assert resp_b_waterfall.json()["total_living_expenses_paise"] == 0
