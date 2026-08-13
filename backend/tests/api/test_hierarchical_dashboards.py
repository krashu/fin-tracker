"""Tests for /api/v1/dashboards/hierarchical-spend and hierarchical-trend."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Account, Category, Transaction, User
from app.services.merchant import normalize_merchant


def _make_txn(
    *,
    user_id: UUID,
    account_id: int,
    txn_date: date,
    amount_paise: int,
    fingerprint: str,
    transaction_type: str = "spend",
    category_id: int | None = None,
    confirmed_at: datetime | None = None,
) -> Transaction:
    return Transaction(
        user_id=user_id,
        account_id=account_id,
        date=txn_date,
        amount_paise=amount_paise,
        transaction_type=transaction_type,
        merchant_raw="MERCHANT",
        merchant_normalized=normalize_merchant("MERCHANT"),
        category_id=category_id,
        fingerprint=fingerprint,
        source="manual",
        confirmed_at=confirmed_at or datetime.now(UTC),
    )


def test_hierarchical_spend_empty(
    client: TestClient,
    session: Session,
    seeded_user: User,
) -> None:
    """Empty spend returns zero totals and empty lists."""
    resp = client.get("/api/v1/dashboards/hierarchical-spend?month=2026-05")
    assert resp.status_code == 200
    data = resp.json()
    assert data["period"] == "2026-05"
    assert data["total_spend_paise"] == 0
    assert data["parents"] == []
    assert data["top_movers"] == []


def test_hierarchical_spend_parent_and_subcategories(
    client: TestClient,
    session: Session,
    seeded_user: User,
) -> None:
    """Correctly rolls up subcategories into parent categories with direct spend."""
    user_id = seeded_user.id

    acct = Account(
        user_id=user_id,
        name="Bank",
        type="bank",
        currency="INR",
        opening_balance_paise=100000,
    )
    session.add(acct)
    session.flush()

    # Create Parent and Subcategories
    food = Category(
        user_id=user_id,
        name="Food & Dining",
        kind="spend",
        parent_id=None,
        color="#2a78d6",
    )
    session.add(food)
    session.flush()

    groceries = Category(
        user_id=user_id,
        name="Groceries",
        kind="spend",
        parent_id=food.id,
        color="#008300",
    )
    restaurants = Category(
        user_id=user_id,
        name="Restaurants",
        kind="spend",
        parent_id=food.id,
    )
    session.add_all([groceries, restaurants])
    session.flush()

    # Add spend txns in May 2026
    # 1. Groceries: -5000 paise (50 INR)
    # 2. Restaurants: -3000 paise (30 INR)
    # 3. Direct Food: -2000 paise (20 INR)
    t1 = _make_txn(
        user_id=user_id,
        account_id=acct.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-5000,
        category_id=groceries.id,
        fingerprint="fp-1",
    )
    t2 = _make_txn(
        user_id=user_id,
        account_id=acct.id,
        txn_date=date(2026, 5, 15),
        amount_paise=-3000,
        category_id=restaurants.id,
        fingerprint="fp-2",
    )
    t3 = _make_txn(
        user_id=user_id,
        account_id=acct.id,
        txn_date=date(2026, 5, 20),
        amount_paise=-2000,
        category_id=food.id,
        fingerprint="fp-3",
    )
    session.add_all([t1, t2, t3])
    session.commit()

    resp = client.get("/api/v1/dashboards/hierarchical-spend?month=2026-05")
    assert resp.status_code == 200
    data = resp.json()

    assert data["total_spend_paise"] == 10000
    assert len(data["parents"]) == 1

    parent = data["parents"][0]
    assert parent["parent_id"] == food.id
    assert parent["parent_name"] == "Food & Dining"
    assert parent["spend_paise"] == 10000
    assert parent["direct_paise"] == 2000
    assert parent["percentage"] == 100.0

    subcats = {s["category_name"]: s for s in parent["subcategories"]}
    assert "Groceries" in subcats
    assert subcats["Groceries"]["spend_paise"] == 5000
    assert subcats["Groceries"]["percentage"] == 50.0

    assert "Restaurants" in subcats
    assert subcats["Restaurants"]["spend_paise"] == 3000
    assert subcats["Restaurants"]["percentage"] == 30.0

    assert "Food & Dining (Direct)" in subcats
    assert subcats["Food & Dining (Direct)"]["spend_paise"] == 2000
    assert subcats["Food & Dining (Direct)"]["is_direct"] is True


def test_hierarchical_spend_tenant_isolation(
    client: TestClient,
    session: Session,
    seeded_user: User,
) -> None:
    """User 2 data never leaks into User 1 hierarchical spend."""
    user1_id = seeded_user.id
    user2_id = uuid.uuid4()
    u2 = User(
        id=user2_id,
        email="user2@example.com",
        password_hash="fake",
    )
    session.add(u2)
    session.flush()

    a1 = Account(
        user_id=user1_id,
        name="A1",
        type="bank",
        currency="INR",
        opening_balance_paise=0,
    )
    a2 = Account(
        user_id=user2_id,
        name="A2",
        type="bank",
        currency="INR",
        opening_balance_paise=0,
    )
    session.add_all([a1, a2])
    session.flush()

    c1 = Category(user_id=user1_id, name="User1 Cat", kind="spend")
    c2 = Category(user_id=user2_id, name="User2 Secret Cat", kind="spend")
    session.add_all([c1, c2])
    session.flush()

    t1 = _make_txn(
        user_id=user1_id,
        account_id=a1.id,
        txn_date=date(2026, 5, 5),
        amount_paise=-1000,
        category_id=c1.id,
        fingerprint="fp-u1",
    )
    t2 = _make_txn(
        user_id=user2_id,
        account_id=a2.id,
        txn_date=date(2026, 5, 5),
        amount_paise=-9999,
        category_id=c2.id,
        fingerprint="fp-u2",
    )
    session.add_all([t1, t2])
    session.commit()

    resp = client.get("/api/v1/dashboards/hierarchical-spend?month=2026-05")
    assert resp.status_code == 200
    data = resp.json()

    assert data["total_spend_paise"] == 1000
    parent_names = [p["parent_name"] for p in data["parents"]]
    assert "User1 Cat" in parent_names
    assert "User2 Secret Cat" not in parent_names


def test_hierarchical_trend_stacked(
    client: TestClient,
    session: Session,
    seeded_user: User,
) -> None:
    """Stacked monthly series returns dense zero-filled buckets with totals."""
    user_id = seeded_user.id

    acct = Account(
        user_id=user_id,
        name="B",
        type="bank",
        currency="INR",
        opening_balance_paise=0,
    )
    session.add(acct)
    session.flush()

    cat = Category(user_id=user_id, name="Utilities", kind="spend", color="#d95926")
    session.add(cat)
    session.flush()

    t1 = _make_txn(
        user_id=user_id,
        account_id=acct.id,
        txn_date=date(2026, 1, 15),
        amount_paise=-4000,
        category_id=cat.id,
        fingerprint="fp-jan",
    )
    t2 = _make_txn(
        user_id=user_id,
        account_id=acct.id,
        txn_date=date(2026, 3, 10),
        amount_paise=-6000,
        category_id=cat.id,
        fingerprint="fp-mar",
    )
    session.add_all([t1, t2])
    session.commit()

    url = "/api/v1/dashboards/hierarchical-trend?bucket=month&start=2026-01-01&end=2026-03-31"
    resp = client.get(url)
    assert resp.status_code == 200
    data = resp.json()

    assert len(data["parents"]) == 1
    assert data["parents"][0]["parent_name"] == "Utilities"

    # Dense zero-fill for Jan, Feb, Mar
    periods = [b["period"] for b in data["buckets"]]
    assert periods == ["2026-01", "2026-02", "2026-03"]

    jan_bucket = data["buckets"][0]
    assert jan_bucket["totals"][0]["total_paise"] == -4000

    feb_bucket = data["buckets"][1]
    assert feb_bucket["totals"][0]["total_paise"] == 0

    mar_bucket = data["buckets"][2]
    assert mar_bucket["totals"][0]["total_paise"] == -6000


def test_hierarchical_spend_refund_netting(
    client: TestClient,
    session: Session,
    seeded_user: User,
) -> None:
    """Refunds net against spends within the same subcategory."""
    user_id = seeded_user.id

    acct = Account(
        user_id=user_id,
        name="Card",
        type="credit_card",
        currency="INR",
        opening_balance_paise=0,
    )
    session.add(acct)
    session.flush()

    cat = Category(user_id=user_id, name="Electronics", kind="spend")
    session.add(cat)
    session.flush()

    # Spend: -10000 paise (100 INR), Refund: +3000 paise (30 INR)
    t1 = _make_txn(
        user_id=user_id,
        account_id=acct.id,
        txn_date=date(2026, 5, 1),
        amount_paise=-10000,
        category_id=cat.id,
        fingerprint="fp-spend",
    )
    t2 = _make_txn(
        user_id=user_id,
        account_id=acct.id,
        txn_date=date(2026, 5, 5),
        amount_paise=3000,
        category_id=cat.id,
        fingerprint="fp-refund",
    )
    session.add_all([t1, t2])
    session.commit()

    resp = client.get("/api/v1/dashboards/hierarchical-spend?month=2026-05")
    assert resp.status_code == 200
    data = resp.json()

    assert data["total_spend_paise"] == 7000
    assert len(data["parents"]) == 1
    assert data["parents"][0]["spend_paise"] == 7000
    assert data["parents"][0]["total_paise"] == -7000


def test_hierarchical_spend_top_movers_calculation(
    client: TestClient,
    session: Session,
    seeded_user: User,
) -> None:
    """Correctly calculates delta and growth rate between previous and current month."""
    user_id = seeded_user.id

    acct = Account(
        user_id=user_id,
        name="B",
        type="bank",
        currency="INR",
        opening_balance_paise=0,
    )
    session.add(acct)
    session.flush()

    parent = Category(user_id=user_id, name="Shopping", kind="spend")
    session.add(parent)
    session.flush()

    clothing = Category(user_id=user_id, name="Clothing", kind="spend", parent_id=parent.id)
    shoes = Category(user_id=user_id, name="Shoes", kind="spend", parent_id=parent.id)
    session.add_all([clothing, shoes])
    session.flush()

    # April (Previous Month): Clothing -2000, Shoes -5000
    t_apr_1 = _make_txn(
        user_id=user_id,
        account_id=acct.id,
        txn_date=date(2026, 4, 10),
        amount_paise=-2000,
        category_id=clothing.id,
        fingerprint="fp-apr-1",
    )
    t_apr_2 = _make_txn(
        user_id=user_id,
        account_id=acct.id,
        txn_date=date(2026, 4, 15),
        amount_paise=-5000,
        category_id=shoes.id,
        fingerprint="fp-apr-2",
    )

    # May (Current Month): Clothing -5000 (+150% growth), Shoes -2000 (-60% contraction)
    t_may_1 = _make_txn(
        user_id=user_id,
        account_id=acct.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-5000,
        category_id=clothing.id,
        fingerprint="fp-may-1",
    )
    t_may_2 = _make_txn(
        user_id=user_id,
        account_id=acct.id,
        txn_date=date(2026, 5, 15),
        amount_paise=-2000,
        category_id=shoes.id,
        fingerprint="fp-may-2",
    )

    session.add_all([t_apr_1, t_apr_2, t_may_1, t_may_2])
    session.commit()

    resp = client.get("/api/v1/dashboards/hierarchical-spend?month=2026-05")
    assert resp.status_code == 200
    data = resp.json()

    movers = {m["category_name"]: m for m in data["top_movers"]}
    assert "Clothing" in movers
    assert movers["Clothing"]["current_paise"] == 5000
    assert movers["Clothing"]["previous_paise"] == 2000
    assert movers["Clothing"]["delta_paise"] == 3000
    assert movers["Clothing"]["growth_rate"] == 150.0

    assert "Shoes" in movers
    assert movers["Shoes"]["current_paise"] == 2000
    assert movers["Shoes"]["previous_paise"] == 5000
    assert movers["Shoes"]["delta_paise"] == -3000
    assert movers["Shoes"]["growth_rate"] == -60.0


def test_hierarchical_invalid_window_422(
    client: TestClient,
    session: Session,
    seeded_user: User,
) -> None:
    """Invalid period shapes return 422 Unprocessable Content."""
    resp = client.get("/api/v1/dashboards/hierarchical-spend?month=invalid")
    assert resp.status_code == 422

    url = "/api/v1/dashboards/hierarchical-trend?bucket=month&start=2026-05-31&end=2026-05-01"
    resp2 = client.get(url)
    assert resp2.status_code == 422
