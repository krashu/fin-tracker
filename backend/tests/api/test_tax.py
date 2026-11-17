"""End-to-end tests for ``/api/v1/tax`` (PRD §F11): the intraday listing and the year picker.

Trades go in through ``/investment-transactions`` so the listing is checked against what the
real write path persists. Cross-user isolation lives in ``test_auth_isolation.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.core import clock
from app.models import User

_URL = "/api/v1/tax/intraday-trades"


def _instrument(client: TestClient, symbol: str = "EQX", asset_class: str = "indian_equity") -> int:
    r = client.post(
        "/api/v1/instruments",
        json={
            "symbol": symbol,
            "name": "Eq X",
            "asset_class": asset_class,
            "exchange": "NSE" if asset_class == "indian_equity" else "MFCentral",
            "current_nav": "130",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _post(
    client: TestClient,
    iid: int,
    kind: str,
    on: str,
    units: str,
    price: str,
    amount: int,
    fees: int = 0,
) -> None:
    r = client.post(
        "/api/v1/investment-transactions",
        json={
            "date": on,
            "instrument_id": iid,
            "transaction_type": kind,
            "units": units,
            "price_per_unit_native": price,
            "amount_native_paise": amount,
            "fees_native_paise": fees,
        },
    )
    assert r.status_code == 201, r.text


def _seed_intraday(client: TestClient) -> None:
    iid = _instrument(client)
    _post(client, iid, "buy", "2026-03-02", "100", "100", 1_000_000)
    _post(client, iid, "buy", "2026-03-03", "50", "120", 600_000)
    _post(client, iid, "sell", "2026-03-03", "80", "130", 1_040_000, fees=800)


def test_intraday_trades_wire_shape(client: TestClient, seeded_user: User) -> None:
    _seed_intraday(client)
    resp = client.get(f"{_URL}?year=2025")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"period", "trades", "total_profit_paise"}
    assert body["period"] == "2025"
    assert len(body["trades"]) == 1
    trade = body["trades"][0]
    assert set(trade) == {
        "sell_txn_id",
        "buy_txn_id",
        "instrument_id",
        "symbol",
        "name",
        "date",
        "units",
        "buy_value_paise",
        "sell_value_paise",
        "sell_charges_paise",
        "profit_paise",
    }
    assert trade["units"] == "50"
    assert trade["date"] == "2026-03-03"
    for k in ("buy_value_paise", "sell_value_paise", "sell_charges_paise", "profit_paise"):
        assert isinstance(trade[k], int)
    assert trade["profit_paise"] == 49_500
    assert body["total_profit_paise"] == 49_500


@pytest.mark.parametrize("year", ["26", "0000", "2026-27", "abcd"])
def test_intraday_trades_year_must_be_yyyy_422(
    client: TestClient, seeded_user: User, year: str
) -> None:
    resp = client.get(f"{_URL}?year={year}")
    assert resp.status_code == 422
    assert resp.json()["detail"] == "year must match YYYY"


def test_tax_available_years_span_realised_sales_and_the_current_fy(
    client: TestClient, seeded_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(clock, "utcnow", lambda: datetime(2026, 11, 15, 6, 0, tzinfo=UTC))
    iid = _instrument(client, symbol="MFX", asset_class="indian_mf")
    _post(client, iid, "buy", "2023-06-01", "10", "100", 100_000)
    _post(client, iid, "sell", "2023-06-02", "10", "110", 110_000)
    resp = client.get("/api/v1/tax/available-years")
    assert resp.status_code == 200
    assert resp.json()["years"] == [2026, 2025, 2024, 2023]


def test_tax_available_years_stay_on_the_financial_year(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tax years are FY start years even though the spend dashboards use calendar years
    (PRD §F8 / §F11): on 15 February 2027 the current tax year is FY 2026-27 -> 2026."""
    monkeypatch.setattr(clock, "utcnow", lambda: datetime(2027, 2, 15, 6, 0, tzinfo=UTC))
    resp = client.get("/api/v1/tax/available-years")
    assert resp.status_code == 200
    assert resp.json()["years"] == [2026]


def test_tax_requires_auth(unauth_client: TestClient) -> None:
    assert unauth_client.get(f"{_URL}?year=2025").status_code == 401
    assert unauth_client.get("/api/v1/tax/available-years").status_code == 401
