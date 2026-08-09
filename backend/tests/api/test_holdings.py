"""End-to-end tests for ``GET /api/v1/holdings`` (PRD §F7).

Integration through the router + holdings_service: decimal-as-string
serialization, money-as-int, and the NAV-None → null path. The FIFO maths are
unit-tested in ``tests/services/test_holdings_service.py``; here we just confirm
the wire shape and that the route composes the service correctly.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.models import Instrument, User
from app.schemas.performance import STALENESS_WARN_DAYS


def test_empty(client: TestClient, seeded_user: User) -> None:
    resp = client.get("/api/v1/holdings")
    assert resp.status_code == 200
    assert resp.json() == {"holdings": []}


def test_holding_after_buy(client: TestClient, seeded_user: User, instrument: Instrument) -> None:
    # instrument fixture has current_nav = 150.
    client.post(
        "/api/v1/investment-transactions",
        json={
            "date": "2026-06-01",
            "instrument_id": instrument.id,
            "transaction_type": "buy",
            "units": "10",
            "price_per_unit_native": "100",
            "amount_native_paise": 100_000,
        },
    )
    body = client.get("/api/v1/holdings").json()
    (h,) = body["holdings"]
    assert h["instrument_id"] == instrument.id
    assert h["symbol"] == "INF209K01YV4"
    assert h["currency"] == "INR"
    assert h["net_units"] == "10"  # decimal-as-string
    assert h["avg_cost_native"] == "100"
    assert h["invested_native_paise"] == 100_000  # money-as-int
    assert h["current_nav"] == "150"
    assert h["current_value_native_paise"] == 150_000
    assert h["unrealized_pnl_native_paise"] == 50_000


def test_holding_without_nav_nulls_value(client: TestClient, seeded_user: User) -> None:
    iid = client.post(
        "/api/v1/instruments",
        json={
            "symbol": "NONAV",
            "name": "No NAV Fund",
            "asset_class": "indian_mf",
            "exchange": "MFCentral",
        },
    ).json()["id"]
    client.post(
        "/api/v1/investment-transactions",
        json={
            "date": "2026-06-01",
            "instrument_id": iid,
            "transaction_type": "buy",
            "units": "10",
            "price_per_unit_native": "100",
            "amount_native_paise": 100_000,
        },
    )
    (h,) = client.get("/api/v1/holdings").json()["holdings"]
    assert h["current_nav"] is None
    assert h["current_value_native_paise"] is None
    assert h["unrealized_pnl_native_paise"] is None
    assert h["invested_native_paise"] == 100_000
    # No price ⇒ no valuation to be stale. 0 would read as "priced, and current".
    assert h["nav_staleness_days"] is None


def test_valuation_age_reaches_the_wire(
    client: TestClient, seeded_user: User, instrument: Instrument
) -> None:
    """The route supplies the ``as_of`` anchor, so the age is server-computed.

    Without it ``compute_holdings`` defaults to ``None`` and /holdings would ship the
    field always-null while the data to fill it sat one argument away — the same
    computed-then-dropped failure this step exists to end. The ``instrument`` fixture
    dates its NAV five days back relative to ``clock.today()``, so the answer is exactly 5
    on any run, and 5 is past ``STALENESS_WARN_DAYS`` — this is a row the UI must tint.
    """
    client.post(
        "/api/v1/investment-transactions",
        json={
            "date": "2026-06-01",
            "instrument_id": instrument.id,
            "transaction_type": "buy",
            "units": "10",
            "price_per_unit_native": "100",
            "amount_native_paise": 100_000,
        },
    )
    (h,) = client.get("/api/v1/holdings").json()["holdings"]
    assert h["nav_staleness_days"] == 5
    assert h["nav_staleness_days"] >= STALENESS_WARN_DAYS
