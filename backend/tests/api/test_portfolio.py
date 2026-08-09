"""End-to-end tests for ``GET /api/v1/portfolio/summary`` (PRD §F8 view 6 / §F9).

Integration through the router + portfolio_service: confirms the wire shape —
money tiles as ints, ``xirr`` as a JSON number-or-null, and the allocation /
holding_xirr arrays. The XIRR maths and partition logic are unit-tested in
``tests/services/test_portfolio_service.py``; the exact XIRR value is not asserted
here because the route values as of ``clock.today()`` (still nondeterministic at runtime —
UTC rather than the host's local date, but it still advances).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core import clock
from app.models import FxRateQuote, Instrument, InvestmentTransaction, User


def test_empty_summary(client: TestClient, seeded_user: User) -> None:
    resp = client.get("/api/v1/portfolio/summary")
    assert resp.status_code == 200
    assert resp.json() == {
        "current_value_paise": 0,
        "invested_paise": 0,
        "unrealized_pnl_paise": 0,
        "xirr": None,
        "holdings_count": 0,
        "null_nav_count": 0,
        "fx_unavailable_count": 0,
        "allocations": [],
        "holding_xirr": [],
    }


def test_summary_wire_shape_after_buy(
    client: TestClient, seeded_user: User, instrument: Instrument
) -> None:
    # instrument fixture has current_nav = 150. Buy dated well in the past so the
    # XIRR is solvable for any plausible run date.
    client.post(
        "/api/v1/investment-transactions",
        json={
            "date": "2020-01-01",
            "instrument_id": instrument.id,
            "transaction_type": "buy",
            "units": "10",
            "price_per_unit_native": "100",
            "amount_native_paise": 100_000,
        },
    )

    resp = client.get("/api/v1/portfolio/summary")
    assert resp.status_code == 200
    body = resp.json()

    # Money tiles: NAV-bearing rollup, all ints.
    assert body["current_value_paise"] == 150_000
    assert body["invested_paise"] == 100_000
    assert body["unrealized_pnl_paise"] == 50_000
    assert all(isinstance(body[k], int) for k in ("current_value_paise", "holdings_count"))
    assert body["holdings_count"] == 1
    assert body["null_nav_count"] == 0

    # xirr is a JSON number or null.
    assert body["xirr"] is None or isinstance(body["xirr"], (int, float))

    # One allocation row per asset class; value as int.
    assert body["allocations"] == [{"asset_class": "indian_mf", "value_paise": 150_000}]

    # One holding_xirr entry per NAV-bearing holding, keyed by instrument_id.
    assert len(body["holding_xirr"]) == 1
    hx = body["holding_xirr"][0]
    assert hx["instrument_id"] == instrument.id
    assert hx["xirr"] is None or isinstance(hx["xirr"], (int, float))


def test_summary_as_of_comes_from_the_utc_clock(
    client: TestClient,
    seeded_user: User,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route's ``as_of`` is ``clock.today()`` (UTC), not the host's local date.

    Proven through the FX read: the only cached USD→INR rate is dated on the *pinned*
    clock's date, and ``rate_on`` is ``date <= as_of``. If the route still read
    ``date.today()`` the as_of would be the real local date, the 2031 rate would fall
    outside the predicate, and the priced USD holding would drop out as FX-unavailable —
    so this fails loudly rather than only near midnight on a positive-offset host.

    This is the only test that observes the route's clock at all: the staleness/as_of
    behaviour is otherwise unit-tested by passing ``as_of`` in explicitly, which cannot
    see which clock the route chose.
    """
    monkeypatch.setattr(clock, "utcnow", lambda: datetime(2031, 5, 17, 4, 30, tzinfo=UTC))

    usd = Instrument(
        user_id=seeded_user.id,
        symbol="VOO",
        name="Vanguard S&P 500",
        asset_class="us_etf",
        currency="USD",
        exchange="NYSE",
        current_nav=Decimal("150"),
    )
    session.add(usd)
    session.flush()
    # Constructed directly rather than via POST: the API stamps fx_rate_to_inr itself from
    # the transaction date, which would need its own cached rate and is not what's under
    # test here. Mirrors tests/api/test_dashboards.py's _usd_instrument_with_buy.
    session.add(
        InvestmentTransaction(
            user_id=seeded_user.id,
            instrument_id=usd.id,
            date=date(2020, 1, 1),
            transaction_type="buy",
            units=Decimal("10"),
            price_per_unit_native=Decimal("100"),
            amount_native_paise=100_000,
            fees_native_paise=0,
            fx_rate_to_inr=Decimal("80"),
        )
    )
    session.add(
        FxRateQuote(
            date=clock.today(),  # == the pinned date; unreachable by the real local date
            from_currency="USD",
            to_currency="INR",
            rate=Decimal("83"),
            source="seed",
        )
    )
    session.commit()

    body = client.get("/api/v1/portfolio/summary").json()
    # 10 units × NAV 150 × 100 cents = 150000, × 83 → INR paise. Non-zero is the point.
    assert body["current_value_paise"] == 150_000 * 83
    assert body["fx_unavailable_count"] == 0
