"""Portfolio assessment and analytics engine (ADR-0021, PRD §F7/§F8/§F9).

Pure Python implementation using `pyxirr` (no Polars dependency):
1. Daily portfolio valuation series (value, net invested, gain)
2. True Daily Time-Weighted Return (TWR)
3. Money-weighted annual return (XIRR)
4. Counterfactual benchmark replay (alpha vs Nifty 50 / S&P 500)
5. Max drawdown & annualized volatility
6. Monthly returns heatmap
7. Family member attribution
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from pyxirr import xirr as _pyxirr_xirr
from sqlalchemy.orm import Session

from app.core import clock
from app.core.log_config import get_logger
from app.models.market_pricing import MarketInstrument
from app.services.market_pricing import (
    filled_series,
    get_instrument,
    splits_for,
)

logger = get_logger(__name__)

_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class TradeItem:
    member: str
    price_key: str
    trade_date: date
    units: float  # + bought, - sold
    amount: float  # + paid in (cash outflow from investor), - taken out


@dataclass(frozen=True, slots=True)
class DailyValuationPoint:
    day: date
    value: float
    net_invested: float
    gain: float
    daily_return: float  # R_t
    cumulative_twr: float


@dataclass(frozen=True, slots=True)
class BenchmarkReplayPoint:
    day: date
    units: float
    value: float
    price: float


@dataclass(frozen=True, slots=True)
class MemberAttribution:
    member: str
    current_value: float
    net_invested: float
    gain: float
    xirr: float | None
    weight: float  # share of total portfolio (0.0 to 1.0)


@dataclass(frozen=True, slots=True)
class MonthlyReturn:
    year: int
    month: int
    return_pct: float


@dataclass(slots=True)
class PortfolioReport:
    start_date: date
    end_date: date
    current_value: float
    net_invested: float
    total_gain: float
    xirr: float | None
    twr_cumulative: float
    twr_annualized: float | None
    max_drawdown: float
    volatility_annualized: float | None
    benchmark_key: str | None = None
    benchmark_current_value: float | None = None
    benchmark_xirr: float | None = None
    alpha: float | None = None
    daily_series: list[DailyValuationPoint] = field(default_factory=list)
    monthly_returns: list[MonthlyReturn] = field(default_factory=list)
    member_attributions: list[MemberAttribution] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def compute_xirr(dates: Sequence[date], amounts: Sequence[float]) -> float | None:
    """Compute annualised XIRR using pyxirr.

    Amounts follow investor perspective: negative for cash invested, positive for receipts.
    """
    cleaned_dates: list[date] = []
    cleaned_amounts: list[float] = []
    for d, a in zip(dates, amounts, strict=False):
        if abs(a) > _EPS:
            cleaned_dates.append(d)
            cleaned_amounts.append(a)

    if (
        not cleaned_amounts
        or all(a > 0 for a in cleaned_amounts)
        or all(a < 0 for a in cleaned_amounts)
    ):
        return None
    try:
        val = _pyxirr_xirr(cleaned_dates, cleaned_amounts)
        if val is None or math.isnan(val) or math.isinf(val):
            return None
        if val < -0.9999 or val > 100.0:
            return None
        return float(val)
    except Exception:
        return None


def calculate_portfolio_report(
    session: Session,
    trades: Sequence[TradeItem],
    *,
    end: date | None = None,
    benchmark_key: str | None = "INDEX:NSEI",
    base_currency: str = "INR",
    apply_splits: bool = True,
) -> PortfolioReport:
    """Analyze a portfolio given a stream of trades and reference pricing."""
    end_date = end or clock.today()
    if not trades:
        return PortfolioReport(
            start_date=end_date,
            end_date=end_date,
            current_value=0.0,
            net_invested=0.0,
            total_gain=0.0,
            xirr=None,
            twr_cumulative=0.0,
            twr_annualized=None,
            max_drawdown=0.0,
            volatility_annualized=None,
            benchmark_key=benchmark_key,
        )

    valid_trades = [t for t in trades if t.trade_date <= end_date]
    if not valid_trades:
        return PortfolioReport(
            start_date=end_date,
            end_date=end_date,
            current_value=0.0,
            net_invested=0.0,
            total_gain=0.0,
            xirr=None,
            twr_cumulative=0.0,
            twr_annualized=None,
            max_drawdown=0.0,
            volatility_annualized=None,
            benchmark_key=benchmark_key,
        )

    start_date = min(t.trade_date for t in valid_trades)
    distinct_keys = sorted({t.price_key for t in valid_trades})
    all_keys = set(distinct_keys)
    if benchmark_key:
        all_keys.add(benchmark_key)

    # Preload instruments, splits, and filled daily series
    instruments: dict[str, MarketInstrument] = {}
    splits_by_key: dict[str, list[Any]] = {}
    prices_by_key: dict[str, dict[date, float]] = {}
    notes: list[str] = []

    for key in all_keys:
        inst = get_instrument(session, key)
        if inst is None:
            notes.append(f"{key}: no market prices cached")
            continue
        instruments[key] = inst
        if apply_splits:
            splits_by_key[key] = splits_for(session, inst.id)
        series = filled_series(session, inst.id, start_date, end_date)
        prices_by_key[key] = {p.day: p.nav_micros / 10_000.0 for p in series}

    # Fallback to last known trade price for instruments without market data
    last_trade_price: dict[str, float] = {}
    for t in sorted(valid_trades, key=lambda x: x.trade_date):
        if abs(t.units) > _EPS:
            last_trade_price[t.price_key] = abs(t.amount / t.units)

    # Build daily calendar positions
    # Calculate net units per day per instrument (adjusting for splits)
    # Group trades by date and key
    trades_by_date: dict[date, list[TradeItem]] = defaultdict(list)
    for t in valid_trades:
        trades_by_date[t.trade_date].append(t)

    current_units: dict[str, float] = defaultdict(float)
    daily_points: list[DailyValuationPoint] = []

    cum_net_invested = 0.0
    prev_value = 0.0
    cum_twr_factor = 1.0
    daily_returns: list[float] = []

    curr_day = start_date
    one_day = timedelta(days=1)

    while curr_day <= end_date:
        day_inflow = 0.0
        # Process trades on this day
        if curr_day in trades_by_date:
            for t in trades_by_date[curr_day]:
                current_units[t.price_key] += t.units
                day_inflow += t.amount
                cum_net_invested += t.amount

        # Value current units
        day_value = 0.0
        for key, units in current_units.items():
            if abs(units) <= _EPS:
                continue
            # Get price
            price = prices_by_key.get(key, {}).get(curr_day)
            if price is None:
                price = last_trade_price.get(key, 0.0)
            day_value += units * price

        # Calculate True Daily TWR
        # R_t = (V_t - C_t) / V_{t-1}
        # where C_t is day_inflow
        daily_r = (day_value - day_inflow) / prev_value - 1.0 if prev_value > _EPS else 0.0

        daily_returns.append(daily_r)
        cum_twr_factor *= 1.0 + daily_r
        gain = day_value - cum_net_invested

        daily_points.append(
            DailyValuationPoint(
                day=curr_day,
                value=day_value,
                net_invested=cum_net_invested,
                gain=gain,
                daily_return=daily_r,
                cumulative_twr=cum_twr_factor - 1.0,
            )
        )

        prev_value = day_value
        curr_day += one_day

    # XIRR Calculation
    # Cash flows: on trade dates, negative of amount; on end date, + closing value
    xirr_dates: list[date] = []
    xirr_amounts: list[float] = []
    for t in valid_trades:
        xirr_dates.append(t.trade_date)
        xirr_amounts.append(-t.amount)
    if prev_value > _EPS:
        xirr_dates.append(end_date)
        xirr_amounts.append(prev_value)

    portfolio_xirr = compute_xirr(xirr_dates, xirr_amounts)

    # Max Drawdown
    peak = 0.0
    max_dd = 0.0
    for p in daily_points:
        if p.value > peak:
            peak = p.value
        elif peak > _EPS:
            dd = (p.value - peak) / peak
            if dd < max_dd:
                max_dd = dd

    # Annualized Volatility
    # Standard deviation of daily returns * sqrt(252)
    if len(daily_returns) > 1:
        mean_r = sum(daily_returns) / len(daily_returns)
        var_r = sum((r - mean_r) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
        annualized_vol: float | None = math.sqrt(var_r * 252.0)
    else:
        annualized_vol = None

    # TWR Annualized
    total_days = (end_date - start_date).days
    cum_twr = cum_twr_factor - 1.0
    if total_days >= 365 and cum_twr > -1.0:
        annualized_twr: float | None = (1.0 + cum_twr) ** (365.0 / total_days) - 1.0
    else:
        annualized_twr = None

    # Benchmark Replay (Alpha)
    benchmark_xirr: float | None = None
    benchmark_value: float | None = None
    alpha: float | None = None

    if benchmark_key and benchmark_key in prices_by_key:
        b_prices = prices_by_key[benchmark_key]
        b_units = 0.0
        for t in valid_trades:
            p = b_prices.get(t.trade_date)
            if p and p > _EPS:
                b_units += t.amount / p

        end_b_price = b_prices.get(end_date)
        if end_b_price and end_b_price > _EPS:
            benchmark_value = b_units * end_b_price
            b_xirr_dates = [t.trade_date for t in valid_trades]
            b_xirr_amounts = [-t.amount for t in valid_trades]
            b_xirr_dates.append(end_date)
            b_xirr_amounts.append(benchmark_value)
            benchmark_xirr = compute_xirr(b_xirr_dates, b_xirr_amounts)
            if portfolio_xirr is not None and benchmark_xirr is not None:
                alpha = portfolio_xirr - benchmark_xirr

    # Monthly Returns Heatmap
    monthly_returns: list[MonthlyReturn] = []
    # Group daily returns by (year, month)
    monthly_factors: dict[tuple[int, int], float] = defaultdict(lambda: 1.0)
    for p in daily_points:
        monthly_factors[(p.day.year, p.day.month)] *= 1.0 + p.daily_return

    for (yr, mo), factor in sorted(monthly_factors.items()):
        monthly_returns.append(MonthlyReturn(year=yr, month=mo, return_pct=factor - 1.0))

    # Member Attribution
    members = sorted({t.member for t in valid_trades})
    member_attributions: list[MemberAttribution] = []
    total_val = prev_value

    for m in members:
        m_trades = [t for t in valid_trades if t.member == m]
        m_units: dict[str, float] = defaultdict(float)
        m_net_inv = 0.0
        for t in m_trades:
            m_units[t.price_key] += t.units
            m_net_inv += t.amount

        m_val = 0.0
        for k, u in m_units.items():
            if abs(u) > _EPS:
                pr = prices_by_key.get(k, {}).get(end_date) or last_trade_price.get(k, 0.0)
                m_val += u * pr

        m_dates = [t.trade_date for t in m_trades]
        m_amounts = [-t.amount for t in m_trades]
        if m_val > _EPS:
            m_dates.append(end_date)
            m_amounts.append(m_val)
        m_xirr = compute_xirr(m_dates, m_amounts)

        weight = m_val / total_val if total_val > _EPS else 0.0
        member_attributions.append(
            MemberAttribution(
                member=m,
                current_value=m_val,
                net_invested=m_net_inv,
                gain=m_val - m_net_inv,
                xirr=m_xirr,
                weight=weight,
            )
        )

    return PortfolioReport(
        start_date=start_date,
        end_date=end_date,
        current_value=prev_value,
        net_invested=cum_net_invested,
        total_gain=prev_value - cum_net_invested,
        xirr=portfolio_xirr,
        twr_cumulative=cum_twr,
        twr_annualized=annualized_twr,
        max_drawdown=max_dd,
        volatility_annualized=annualized_vol,
        benchmark_key=benchmark_key,
        benchmark_current_value=benchmark_value,
        benchmark_xirr=benchmark_xirr,
        alpha=alpha,
        daily_series=daily_points,
        monthly_returns=monthly_returns,
        member_attributions=member_attributions,
        notes=notes,
    )
