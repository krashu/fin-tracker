"""End-to-end API tests for ``GET /api/v1/portfolio/timeseries`` (Milestone 2 Phase 4)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import ACCESS_COOKIE_NAME, create_access_token
from app.models import (
    Instrument,
    InvestmentAccount,
    InvestmentTransaction,
    User,
)
from app.services.market_pricing import PricePoint, ensure_instrument, upsert_prices
from app.services.portfolio_analytics_service import invalidate_portfolio_cache


def test_empty_timeseries(client: TestClient, seeded_user: User) -> None:
    invalidate_portfolio_cache(seeded_user.id)
    resp = client.get("/api/v1/portfolio/timeseries")
    assert resp.status_code == 200
    data = resp.json()
    assert data["scope"] == "all"
    assert data["points"] == []
    assert data["periods"] == []


def test_timeseries_wire_shape_and_points(
    client: TestClient,
    session: Session,
    seeded_user: User,
    instrument: Instrument,
) -> None:
    # Set up market pricing for instrument
    market_inst = ensure_instrument(session, instrument.symbol)
    d1 = date(2025, 1, 1)
    d2 = date(2025, 1, 2)
    upsert_prices(
        session,
        market_inst.id,
        [PricePoint(d1, 100_0000), PricePoint(d2, 110_0000)],
    )
    session.commit()

    # Buy 10 units at 100 on d1
    post_res = client.post(
        "/api/v1/investment-transactions",
        json={
            "date": d1.isoformat(),
            "instrument_id": instrument.id,
            "transaction_type": "buy",
            "units": "10",
            "price_per_unit_native": "100",
            "amount_native_paise": 100000,
        },
    )
    assert post_res.status_code == 201

    invalidate_portfolio_cache(seeded_user.id)
    resp = client.get(
        "/api/v1/portfolio/timeseries",
        params={"from_date": d1.isoformat(), "to_date": d2.isoformat()},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["scope"] == "all"
    assert len(data["points"]) == 2

    pt1 = data["points"][0]
    assert pt1["date"] == d1.isoformat()
    assert pt1["portfolio_value_paise"] == 100000
    assert pt1["invested_capital_paise"] == 100000
    assert pt1["twr_index_micros"] == 1000000  # base 100.0
    assert pt1["is_invested"] is True

    pt2 = data["points"][1]
    assert pt2["date"] == d2.isoformat()
    assert pt2["portfolio_value_paise"] == 110000
    assert pt2["twr_index_micros"] == 1100000  # +10%


def test_timeseries_validation_errors(client: TestClient, seeded_user: User) -> None:
    # 422 if from_date > to_date
    resp = client.get(
        "/api/v1/portfolio/timeseries",
        params={"from_date": "2026-02-01", "to_date": "2026-01-01"},
    )
    assert resp.status_code == 422
    assert "from_date cannot be after to_date" in resp.json()["detail"]

    # 422 if scope is 'account' without account_id
    resp = client.get("/api/v1/portfolio/timeseries", params={"scope": "account"})
    assert resp.status_code == 422
    assert "account_id is required" in resp.json()["detail"]

    # 404 if account_id does not exist
    resp = client.get(
        "/api/v1/portfolio/timeseries",
        params={"scope": "account", "account_id": 999999},
    )
    assert resp.status_code == 404


def test_tenant_isolation_cross_user_account_404(
    client: TestClient,
    session: Session,
    seeded_user: User,
) -> None:
    """ADR-0003: querying with another user's account_id must return 404, never 403 or data."""
    other_uid = uuid.UUID("00000000-0000-0000-0000-000000000099")
    other_user = User(id=other_uid, email="other_iso@example.com", password_hash="dummy")
    session.add(other_user)
    session.flush()

    other_acc = InvestmentAccount(
        user_id=other_uid,
        name="Other Secret Account",
        account_type="demat",
        provider="zerodha",
        external_account_ref="OTHER123",
    )
    session.add(other_acc)
    session.commit()

    # seeded_user querying other_user's account
    resp = client.get(
        "/api/v1/portfolio/timeseries",
        params={"scope": "account", "account_id": other_acc.id},
    )
    assert resp.status_code == 404
    assert f"investment account {other_acc.id} not found" in resp.json()["detail"]


def test_tenant_isolation_data_separation(
    client: TestClient,
    session: Session,
    seeded_user: User,
) -> None:
    """User B cannot see User A's timeseries data under scope='all'."""
    other_uid = uuid.UUID("00000000-0000-0000-0000-000000000088")
    other_user = User(id=other_uid, email="other_data@example.com", password_hash="dummy")
    session.add(other_user)
    session.flush()

    other_inst = Instrument(
        user_id=other_uid,
        symbol="OTHER_SYM",
        name="Other Stock",
        asset_class="indian_equity",
        currency="INR",
        exchange="NSE",
    )
    session.add(other_inst)
    session.flush()

    other_acc = InvestmentAccount(
        user_id=other_uid,
        name="Other Demat",
        account_type="demat",
        provider="zerodha",
        external_account_ref="OTHER_ACC_88",
    )
    session.add(other_acc)
    session.flush()

    d1 = date(2026, 1, 1)
    other_txn = InvestmentTransaction(
        user_id=other_uid,
        account_id=other_acc.id,
        instrument_id=other_inst.id,
        date=d1,
        transaction_type="buy",
        units=Decimal("100"),
        price_per_unit_native=Decimal("100"),
        amount_native_paise=1000000,
        fees_native_paise=0,
        fx_rate_to_inr=Decimal("1.0"),
    )
    session.add(other_txn)
    session.commit()

    # Query as seeded_user: seeded_user has no transactions in other_inst
    invalidate_portfolio_cache(seeded_user.id)
    resp = client.get("/api/v1/portfolio/timeseries")
    assert resp.status_code == 200
    assert resp.json()["points"] == []

    # Query as other_user using access cookie swap
    client.cookies.set(ACCESS_COOKIE_NAME, create_access_token(other_uid))
    invalidate_portfolio_cache(other_uid)
    resp_other = client.get("/api/v1/portfolio/timeseries")
    assert resp_other.status_code == 200
    assert len(resp_other.json()["points"]) >= 1
    assert resp_other.json()["points"][0]["portfolio_value_paise"] == 1000000
