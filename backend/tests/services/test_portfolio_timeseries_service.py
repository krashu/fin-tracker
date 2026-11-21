"""Tests for portfolio timeseries service & financial math engine (Milestone 2 Phase 4)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    Base,
    Instrument,
    InvestmentAccount,
    InvestmentTransaction,
    User,
)
from app.services.market_pricing import (
    PricePoint,
    ensure_instrument,
    upsert_prices,
)
from app.services.portfolio_analytics_service import (
    TradeItem,
    calculate_portfolio_report,
    get_or_compute_portfolio_timeseries,
    invalidate_portfolio_cache,
    invalidate_portfolio_snapshots,
)


@pytest.fixture
def mem_engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def sessions(mem_engine):
    return sessionmaker(bind=mem_engine)


@pytest.fixture
def session(sessions):
    with sessions() as s:
        yield s


def test_asymmetric_bod_inflow_prevents_denominator_blowup(session: Session):
    """Large buy on tiny balance ($V_{d-1}=1k, CF_{net}=10L$) must scale
    against deployed capital.
    """
    inst = ensure_instrument(session, "NSE:TEST_STOCK")
    d0 = date(2026, 1, 1)
    d1 = date(2026, 1, 2)

    # Day 0: 1 unit at 1000
    # Day 1: Price dips to 985 (-1.5%)
    upsert_prices(
        session,
        inst.id,
        [
            PricePoint(d0, 1000_0000),
            PricePoint(d1, 985_0000),
        ],
    )
    session.commit()

    trades = [
        # Day 0: 1 share at 1000
        TradeItem(
            member="Self",
            price_key="NSE:TEST_STOCK",
            trade_date=d0,
            units=1.0,
            amount=1000.0,
        ),
        # Day 1: Large buy of 1000 shares at 1000 each = 10,00,000 INR
        TradeItem(
            member="Self",
            price_key="NSE:TEST_STOCK",
            trade_date=d1,
            units=1000.0,
            amount=1_000_000.0,
        ),
    ]

    report = calculate_portfolio_report(session, trades, end=d1, benchmark_key=None)

    assert len(report.daily_series) == 2
    d1_pt = report.daily_series[1]

    # Day 1 close: 1001 shares * 985 = 985,985
    # Capital deployed: V_{d-1} (1000) + CF_net (1,000,000) = 1,001,000
    # Loss: 985,985 - 1000 - 1,000,000 = -15,015
    # Daily return: -15,015 / 1,001,000 ≈ -1.5% (-0.015)
    # NOT -1,500% (-15.0) which would have inverted 1 + R_d!
    assert d1_pt.daily_return == pytest.approx(-0.015, abs=1e-3)
    assert 1.0 + d1_pt.daily_return > 0.0
    assert d1_pt.cumulative_twr == pytest.approx(-0.015, abs=1e-3)


def test_liquidation_gap_and_reentry(session: Session):
    """TWR index freezes during complete liquidation and resumes compounding on re-entry."""
    inst = ensure_instrument(session, "NSE:LIQ_STOCK")
    d1 = date(2026, 1, 1)
    d2 = date(2026, 1, 2)
    d3 = date(2026, 1, 3)
    d4 = date(2026, 1, 4)
    d5 = date(2026, 1, 5)

    # Prices:
    # d1: 100, d2: 120 (+20% gain, then sold 100%)
    # d3: 130 (dormant, 0 units held)
    # d4: 100 (re-enter with 10 units at 100)
    # d5: 110 (+10% gain on re-entry lot)
    upsert_prices(
        session,
        inst.id,
        [
            PricePoint(d1, 100_0000),
            PricePoint(d2, 120_0000),
            PricePoint(d3, 130_0000),
            PricePoint(d4, 100_0000),
            PricePoint(d5, 110_0000),
        ],
    )
    session.commit()

    trades = [
        # d1: Buy 10 units at 100 = 1000
        TradeItem(
            member="Self", price_key="NSE:LIQ_STOCK", trade_date=d1, units=10.0, amount=1000.0
        ),
        # d2: Sell all 10 units at 120 = 1200
        TradeItem(
            member="Self", price_key="NSE:LIQ_STOCK", trade_date=d2, units=-10.0, amount=-1200.0
        ),
        # d4: Re-enter, buy 10 units at 100 = 1000
        TradeItem(
            member="Self", price_key="NSE:LIQ_STOCK", trade_date=d4, units=10.0, amount=1000.0
        ),
    ]

    report = calculate_portfolio_report(session, trades, end=d5, benchmark_key=None)
    series = {p.day: p for p in report.daily_series}

    # d1: Day 1 inception, return = 0
    assert series[d1].daily_return == pytest.approx(0.0)
    assert series[d1].cumulative_twr == pytest.approx(0.0)

    # d2: Sold at 120 (+20%)
    assert series[d2].daily_return == pytest.approx(0.20, abs=1e-3)
    assert series[d2].cumulative_twr == pytest.approx(0.20, abs=1e-3)
    assert series[d2].value == pytest.approx(0.0)

    # d3: Dormant day, value = 0, return = 0, TWR frozen at 20%
    assert series[d3].value == pytest.approx(0.0)
    assert series[d3].daily_return == pytest.approx(0.0)
    assert series[d3].cumulative_twr == pytest.approx(0.20, abs=1e-3)
    assert series[d3].is_invested is False

    # d4: Re-entry day, buys at close price 100, return = 0, TWR stays frozen at 20%
    assert series[d4].value == pytest.approx(1000.0)
    assert series[d4].daily_return == pytest.approx(0.0)
    assert series[d4].cumulative_twr == pytest.approx(0.20, abs=1e-3)
    assert series[d4].is_invested is True

    # d5: Grows from 100 to 110 (+10%)
    # Total cumulative TWR should compound: (1 + 0.20) * (1 + 0.10) - 1 = 1.32 - 1 = 32%
    assert series[d5].daily_return == pytest.approx(0.10, abs=1e-3)
    assert series[d5].cumulative_twr == pytest.approx(0.32, abs=1e-3)


def test_cambridge_mpme_benchmark_replay_guarantees_non_negative(session: Session):
    """Cambridge Associates mPME proportional liquidation maintains non-negative benchmark units."""
    stock = ensure_instrument(session, "NSE:OUTPERFORM")
    bench = ensure_instrument(session, "INDEX:NSEI")

    d1 = date(2026, 1, 1)
    d2 = date(2026, 1, 2)
    d3 = date(2026, 1, 3)

    # Stock grows 5x (100 -> 500), Benchmark grows 1.5x (1000 -> 1500)
    upsert_prices(
        session,
        stock.id,
        [PricePoint(d1, 100_0000), PricePoint(d2, 500_0000), PricePoint(d3, 500_0000)],
    )
    upsert_prices(
        session,
        bench.id,
        [PricePoint(d1, 1000_0000), PricePoint(d2, 1500_0000), PricePoint(d3, 1500_0000)],
    )
    session.commit()

    trades = [
        # d1: Invest 10L (10,000 shares at 100)
        TradeItem(
            member="Self",
            price_key="NSE:OUTPERFORM",
            trade_date=d1,
            units=10_000.0,
            amount=1_000_000.0,
        ),
        # d2: Portfolio value is now 50L. Withdraw 25L in profits (sell 5,000 shares at 500)
        # In Long-Nickels, withdrawing 25L from 15L benchmark shorts it by -10L.
        # Under Cambridge mPME, withdrawing 50% of portfolio sells 50% of benchmark units.
        TradeItem(
            member="Self",
            price_key="NSE:OUTPERFORM",
            trade_date=d2,
            units=-5_000.0,
            amount=-2_500_000.0,
        ),
    ]

    report = calculate_portfolio_report(session, trades, end=d3, benchmark_key="INDEX:NSEI")
    series = {p.day: p for p in report.daily_series}

    # d2 benchmark value must be positive
    assert series[d2].benchmark_value is not None
    assert series[d2].benchmark_value > 0.0

    # Benchmark initial units = 10L / 1000 = 1000 units.
    # At d2: benchmark before withdrawal = 1000 * 1500 = 15L.
    # Withdrawal of 25L out of 50L portfolio = 50% withdrawal fraction.
    # mPME sells 50% of benchmark units -> 500 units left.
    # 500 units * 1500 = 7.5L benchmark value!
    assert series[d2].benchmark_value == pytest.approx(750_000.0, rel=1e-3)
    assert series[d3].benchmark_value == pytest.approx(750_000.0, rel=1e-3)


def test_portfolio_timeseries_get_and_cache(session: Session):
    """Test get_or_compute_portfolio_timeseries with user DB rows, cache, and invalidation."""
    u_id = uuid.uuid4()
    user = User(id=u_id, email=f"test_{u_id.hex[:8]}@example.com", password_hash="dummy")
    session.add(user)
    session.flush()

    acc = InvestmentAccount(
        user_id=u_id,
        name="Zerodha Demat",
        account_type="demat",
        provider="zerodha",
        external_account_ref="DEMAT123",
        currency="INR",
    )
    session.add(acc)
    session.flush()

    inst = Instrument(
        user_id=u_id,
        symbol="HDFCBANK",
        name="HDFC Bank Ltd",
        asset_class="indian_equity",
        currency="INR",
        exchange="NSE",
    )
    session.add(inst)
    session.flush()

    market_inst = ensure_instrument(session, "NSE:HDFCBANK")
    d1 = date(2026, 1, 1)
    d2 = date(2026, 1, 2)
    upsert_prices(
        session,
        market_inst.id,
        [PricePoint(d1, 1500_0000), PricePoint(d2, 1650_0000)],
    )
    session.commit()

    txn = InvestmentTransaction(
        user_id=u_id,
        account_id=acc.id,
        instrument_id=inst.id,
        date=d1,
        transaction_type="buy",
        units=Decimal("10"),
        price_per_unit_native=Decimal("1500"),
        amount_native_paise=1500000,
        fees_native_paise=0,
        fx_rate_to_inr=Decimal("1.0"),
    )
    session.add(txn)
    session.commit()

    # First fetch: computes and caches
    invalidate_portfolio_cache(u_id)
    resp = get_or_compute_portfolio_timeseries(
        session,
        user_id=u_id,
        scope_type="all",
        to_date=d2,
        benchmark_key=None,
        persist=True,
    )

    assert resp.scope == "all"
    assert len(resp.points) == 2
    assert resp.points[0].portfolio_value_paise == 1500000
    assert resp.points[1].portfolio_value_paise == 1650000
    # twr index at d1 is 100.0 (1,000,000 micros), at d2 is +10% -> 110.0 (1,100,000 micros)
    assert resp.points[0].twr_index_micros == 1_000_000
    assert resp.points[1].twr_index_micros == 1_100_000

    # Invalidate snapshots from d2
    pruned = invalidate_portfolio_snapshots(session, user_id=u_id, from_date=d2)
    assert pruned >= 1
