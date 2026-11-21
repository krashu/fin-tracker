"""Unit tests for portfolio analytics engine (ADR-0021)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.services.market_pricing import (
    PricePoint,
    ensure_instrument,
    upsert_prices,
)
from app.services.portfolio_analytics_service import (
    TradeItem,
    calculate_portfolio_report,
    compute_xirr,
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


def test_compute_xirr():
    # Buy 100 on 2025-01-01, sell 110 on 2026-01-01 -> ~10% XIRR
    d1 = date(2025, 1, 1)
    d2 = date(2026, 1, 1)
    r = compute_xirr([d1, d2], [-100.0, 110.0])
    assert r is not None
    assert r == pytest.approx(0.10, rel=1e-2)

    # All positive flows -> None
    assert compute_xirr([d1, d2], [10.0, 20.0]) is None

    # Empty -> None
    assert compute_xirr([], []) is None


def test_portfolio_valuation_and_twr(session: Session):
    # Setup test instrument
    inst = ensure_instrument(session, "NSE:TCS")
    d1 = date(2026, 1, 1)
    d2 = date(2026, 1, 2)
    d3 = date(2026, 1, 3)

    # 1 unit at 1000 on d1, 1100 on d2, 1210 on d3 (10% daily growth)
    upsert_prices(
        session,
        inst.id,
        [
            PricePoint(d1, 1000_0000),
            PricePoint(d2, 1100_0000),
            PricePoint(d3, 1210_0000),
        ],
    )
    session.commit()

    trades = [
        TradeItem(
            member="User1",
            price_key="NSE:TCS",
            trade_date=d1,
            units=10.0,
            amount=10_000.0,  # 10 units at 1000
        )
    ]

    report = calculate_portfolio_report(session, trades, end=d3, benchmark_key=None)

    assert report.start_date == d1
    assert report.end_date == d3
    assert report.current_value == pytest.approx(12_100.0)  # 10 * 1210
    assert report.net_invested == pytest.approx(10_000.0)
    assert report.total_gain == pytest.approx(2_100.0)

    # TWR: from 10,000 to 12,100 with no mid-period cash flows = 21%
    assert report.twr_cumulative == pytest.approx(0.21, rel=1e-3)
    assert report.max_drawdown == pytest.approx(0.0)  # monotonic increase
    assert len(report.daily_series) == 3


def test_portfolio_benchmark_replay(session: Session):
    inst = ensure_instrument(session, "NSE:STOCK")
    bench = ensure_instrument(session, "INDEX:NSEI")

    d1 = date(2025, 1, 1)
    d2 = date(2026, 1, 1)

    upsert_prices(session, inst.id, [PricePoint(d1, 100_0000), PricePoint(d2, 120_0000)])  # +20%
    upsert_prices(session, bench.id, [PricePoint(d1, 1000_0000), PricePoint(d2, 1050_0000)])  # +5%
    session.commit()

    trades = [
        TradeItem(
            member="User1",
            price_key="NSE:STOCK",
            trade_date=d1,
            units=10.0,
            amount=1000.0,
        )
    ]

    report = calculate_portfolio_report(session, trades, end=d2, benchmark_key="INDEX:NSEI")

    assert report.current_value == pytest.approx(1200.0)
    assert report.benchmark_current_value == pytest.approx(1050.0)
    assert report.alpha is not None
    assert report.alpha > 0.0  # stock outperformed benchmark


def test_member_attribution(session: Session):
    inst = ensure_instrument(session, "NSE:INFY")
    d1 = date(2026, 1, 1)
    upsert_prices(session, inst.id, [PricePoint(d1, 100_0000)])
    session.commit()

    trades = [
        TradeItem(
            member="Alice",
            price_key="NSE:INFY",
            trade_date=d1,
            units=10.0,
            amount=1000.0,
        ),
        TradeItem(
            member="Bob",
            price_key="NSE:INFY",
            trade_date=d1,
            units=30.0,
            amount=3000.0,
        ),
    ]

    report = calculate_portfolio_report(session, trades, end=d1, benchmark_key=None)

    assert report.current_value == pytest.approx(4000.0)
    assert len(report.member_attributions) == 2

    alice = next(m for m in report.member_attributions if m.member == "Alice")
    bob = next(m for m in report.member_attributions if m.member == "Bob")

    assert alice.current_value == pytest.approx(1000.0)
    assert alice.weight == pytest.approx(0.25)

    assert bob.current_value == pytest.approx(3000.0)
    assert bob.weight == pytest.approx(0.75)
