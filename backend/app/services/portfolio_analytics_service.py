"""Portfolio assessment and analytics engine (ADR-0021, PRD §F7/§F8/§F9, Milestone 2 Phase 4).

Pure Python implementation using `pyxirr` (no Polars dependency):
1. Daily portfolio valuation series (value, net invested, gain)
2. Asymmetric BOD/EOD True Daily Time-Weighted Return (TWR)
3. Cambridge Associates mPME counterfactual benchmark replay
4. Money-weighted annual return (XIRR)
5. Max drawdown & annualized volatility
6. Period performance breakdown (1M, 3M, 6M, 1Y, 3Y, 5Y, All)
7. Family member / account attribution
8. Write-through snapshot caching & invalidation
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from pyxirr import xirr as _pyxirr_xirr
from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from app.core import clock
from app.core.log_config import get_logger
from app.models.benchmark import Benchmark, BenchmarkNav
from app.models.instrument import Instrument
from app.models.investment_account import InvestmentAccount
from app.models.investment_transaction import InvestmentTransaction
from app.models.market_pricing import MarketInstrument
from app.models.portfolio_daily_snapshot import PortfolioDailySnapshot
from app.schemas.portfolio_timeseries import (
    PeriodPerformance,
    PortfolioTimeseriesPoint,
    PortfolioTimeseriesResponse,
    ScopeTypeStr,
)
from app.services.market_pricing import (
    filled_series,
    get_instrument,
    splits_for,
)

logger = get_logger(__name__)

_EPS = 1e-9

# In-memory epoch cache for timeseries calculation results
_TIMESERIES_CACHE: dict[tuple[Any, ...], tuple[datetime, PortfolioTimeseriesResponse]] = {}
_USER_TX_EPOCHS: dict[UUID, int] = defaultdict(int)


def bump_user_tx_epoch(user_id: UUID) -> None:
    """Increment user transaction epoch to invalidate memory cache."""
    _USER_TX_EPOCHS[user_id] += 1
    to_delete = [k for k in _TIMESERIES_CACHE if k[0] == user_id]
    for k in to_delete:
        _TIMESERIES_CACHE.pop(k, None)


def invalidate_portfolio_cache(user_id: UUID | None = None) -> None:
    """Clear memory cache globally or for a specific user."""
    if user_id is None:
        _TIMESERIES_CACHE.clear()
        _USER_TX_EPOCHS.clear()
    else:
        bump_user_tx_epoch(user_id)


def invalidate_portfolio_snapshots(
    session: Session,
    user_id: UUID,
    from_date: date,
    scope_type: str | None = None,
    scope_id: int | None = None,
) -> int:
    """Prune cached snapshots on or after from_date and bust in-memory epoch cache."""
    bump_user_tx_epoch(user_id)
    stmt = delete(PortfolioDailySnapshot).where(
        PortfolioDailySnapshot.user_id == user_id,
        PortfolioDailySnapshot.snapshot_date >= from_date,
    )
    if scope_type is not None:
        stmt = stmt.where(PortfolioDailySnapshot.scope_type == scope_type)
    if scope_id is not None:
        stmt = stmt.where(PortfolioDailySnapshot.scope_id == scope_id)
    res = session.execute(stmt)
    return int(getattr(res, "rowcount", 0))


@dataclass(frozen=True, slots=True)
class TradeItem:
    member: str
    price_key: str
    trade_date: date
    units: float  # + bought, - sold
    amount: float  # + paid in (cash outflow from investor), - taken out
    fees: float = 0.0
    dividend: float = 0.0


@dataclass(frozen=True, slots=True)
class DailyValuationPoint:
    day: date
    value: float
    net_invested: float
    gain: float
    daily_return: float  # R_t
    cumulative_twr: float
    net_cashflow: float = 0.0
    benchmark_value: float | None = None
    is_invested: bool = True


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


def instrument_to_price_key(inst: Instrument) -> str:
    """Map user-owned Instrument to market pricing price_key."""
    if inst.isin and inst.isin.startswith("INF"):
        return inst.isin
    if inst.symbol.startswith("INF") and len(inst.symbol) == 12:
        return inst.symbol
    if inst.amfi_code and inst.asset_class == "indian_mf":
        return f"AMFI:{inst.amfi_code}"
    if inst.exchange in ("NSE", "BSE", "NASDAQ", "NYSE"):
        return f"{inst.exchange}:{inst.symbol}"
    return inst.symbol


def load_user_trades(
    session: Session,
    user_id: UUID,
    *,
    scope_type: str = "all",
    scope_id: int | None = None,
) -> list[TradeItem]:
    """Load and normalize user investment transactions into a trade stream."""
    inst_stmt = select(Instrument).where(Instrument.user_id == user_id)
    insts = session.scalars(inst_stmt).all()
    inst_map = {i.id: i for i in insts}

    acc_stmt = select(InvestmentAccount).where(InvestmentAccount.user_id == user_id)
    accounts = session.scalars(acc_stmt).all()
    acc_map = {a.id: a.name for a in accounts}

    txn_stmt = select(InvestmentTransaction).where(InvestmentTransaction.user_id == user_id)
    if scope_type == "account" and scope_id is not None and scope_id > 0:
        txn_stmt = txn_stmt.where(InvestmentTransaction.account_id == scope_id)

    txn_stmt = txn_stmt.order_by(InvestmentTransaction.date.asc(), InvestmentTransaction.id.asc())
    txns = session.scalars(txn_stmt).all()

    trades: list[TradeItem] = []
    for t in txns:
        inst = inst_map.get(t.instrument_id)
        if inst is None:
            continue
        key = instrument_to_price_key(inst)
        member = acc_map.get(t.account_id, "Self")

        units = float(t.units)
        fx = float(t.fx_rate_to_inr)
        gross_paise = int(round(t.amount_native_paise * fx))
        fees_paise = int(round(t.fees_native_paise * fx))

        ttype = t.transaction_type
        if ttype in ("buy", "sip", "switch_in"):
            trades.append(
                TradeItem(
                    member=member,
                    price_key=key,
                    trade_date=t.date,
                    units=units,
                    amount=(gross_paise + fees_paise) / 100.0,
                    fees=fees_paise / 100.0,
                    dividend=0.0,
                )
            )
        elif ttype in ("sell", "switch_out"):
            trades.append(
                TradeItem(
                    member=member,
                    price_key=key,
                    trade_date=t.date,
                    units=-units,
                    amount=-(gross_paise - fees_paise) / 100.0,
                    fees=fees_paise / 100.0,
                    dividend=0.0,
                )
            )
        elif ttype == "dividend":
            trades.append(
                TradeItem(
                    member=member,
                    price_key=key,
                    trade_date=t.date,
                    units=0.0,
                    amount=0.0,
                    fees=0.0,
                    dividend=gross_paise / 100.0,
                )
            )
        elif ttype in ("bonus", "split"):
            trades.append(
                TradeItem(
                    member=member,
                    price_key=key,
                    trade_date=t.date,
                    units=units,
                    amount=0.0,
                    fees=0.0,
                    dividend=0.0,
                )
            )

    return trades


def calculate_portfolio_report(
    session: Session,
    trades: Sequence[TradeItem],
    *,
    end: date | None = None,
    benchmark_key: str | None = "INDEX:NSEI",
    benchmark_id: int | None = None,
    base_currency: str = "INR",
    apply_splits: bool = True,
) -> PortfolioReport:
    """Analyze a portfolio given a stream of trades and reference pricing.

    Implements:
    - Asymmetric BOD Inflows / EOD Outflows True Daily TWR (W=1 Inflows, W=0 Outflows).
    - Multi-currency USD -> INR conversion via daily FX rate.
    - Cambridge Associates mPME counterfactual benchmark replay.
    - Continuous frozen TWR linking across zero-holdings liquidation gaps.
    """
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
    # Pre-add FX if needed for USD equities or US benchmarks
    all_keys.add("FX:USDINR")

    # Preload instruments, splits, and filled daily series
    instruments: dict[str, MarketInstrument] = {}
    splits_by_key: dict[str, list[Any]] = {}
    prices_by_key: dict[str, dict[date, float]] = {}
    notes: list[str] = []

    for key in all_keys:
        inst = get_instrument(session, key)
        if inst is None:
            if key != "FX:USDINR":
                notes.append(f"{key}: no market prices cached")
            continue
        instruments[key] = inst
        if apply_splits:
            splits_by_key[key] = splits_for(session, inst.id)
        series = filled_series(session, inst.id, start_date, end_date)
        prices_by_key[key] = {p.day: p.nav_micros / 10_000.0 for p in series}

    # Load benchmark prices from BenchmarkNav if benchmark_id is provided or resolved
    resolved_benchmark_id = benchmark_id
    if resolved_benchmark_id is None and benchmark_key and benchmark_key not in prices_by_key:
        first_b = session.scalar(
            select(Benchmark).where(Benchmark.archived_at.is_(None)).order_by(Benchmark.id.asc())
        )
        if first_b:
            resolved_benchmark_id = first_b.id

    if resolved_benchmark_id is not None:
        nav_rows = session.execute(
            select(BenchmarkNav.nav_date, BenchmarkNav.nav)
            .where(BenchmarkNav.benchmark_id == resolved_benchmark_id)
            .order_by(BenchmarkNav.nav_date.asc())
        ).all()
        if nav_rows:
            target_b_key = benchmark_key or "INDEX:NSEI"
            raw_navs = {r.nav_date: float(r.nav) for r in nav_rows}
            sorted_dates = sorted(raw_navs.keys())
            if sorted_dates:
                b_map: dict[date, float] = {}
                curr_p = raw_navs[sorted_dates[0]]
                cur_d = min(start_date, sorted_dates[0])
                while cur_d <= end_date:
                    if cur_d in raw_navs:
                        curr_p = raw_navs[cur_d]
                    b_map[cur_d] = curr_p
                    cur_d += timedelta(days=1)
                prices_by_key[target_b_key] = b_map

    # Fallback to last known trade price for instruments without market data
    last_trade_price: dict[str, float] = {}
    for t in sorted(valid_trades, key=lambda x: x.trade_date):
        if abs(t.units) > _EPS:
            last_trade_price[t.price_key] = abs(t.amount / t.units)

    # Benchmark preparation (Cambridge Associates mPME)
    b_inst = instruments.get(benchmark_key) if benchmark_key else None
    is_b_usd = b_inst is not None and b_inst.currency == "USD"
    fx_series = prices_by_key.get("FX:USDINR", {})

    def get_b_price_inr(d: date) -> float:
        if not benchmark_key or benchmark_key not in prices_by_key:
            return 0.0
        base_p = prices_by_key[benchmark_key].get(d, 0.0)
        if is_b_usd and base_p > _EPS:
            fx = fx_series.get(d, 84.0)
            return base_p * fx
        return base_p

    # Group trades by date
    trades_by_date: dict[date, list[TradeItem]] = defaultdict(list)
    for t in valid_trades:
        trades_by_date[t.trade_date].append(t)

    current_units: dict[str, float] = defaultdict(float)
    daily_points: list[DailyValuationPoint] = []

    cum_net_invested = 0.0
    prev_value = 0.0
    cum_twr_factor = 1.0
    daily_returns: list[float] = []

    # Benchmark mPME state
    b_units = 0.0
    last_b_price = 0.0

    curr_day = start_date
    one_day = timedelta(days=1)

    while curr_day <= end_date:
        day_net_inflow = 0.0
        day_gross_inflow = 0.0
        day_dividend = 0.0

        # Pre-trade valuation today (V_{d, pre}) using existing units at today's prices
        pre_trade_val = 0.0
        for key, units in current_units.items():
            if abs(units) <= _EPS:
                continue
            pr = prices_by_key.get(key, {}).get(curr_day)
            if pr is None:
                pr = last_trade_price.get(key, 0.0)
            inst_obj = instruments.get(key)
            if inst_obj is not None and inst_obj.currency == "USD":
                pr = pr * fx_series.get(curr_day, 84.0)
            pre_trade_val += units * pr

        # Process trades on this day
        if curr_day in trades_by_date:
            for t in trades_by_date[curr_day]:
                current_units[t.price_key] += t.units
                day_net_inflow += t.amount
                if t.amount > 0:
                    day_gross_inflow += t.amount
                day_dividend += t.dividend
                cum_net_invested += t.amount

        # Value current units in INR
        day_value = 0.0
        for key, units in current_units.items():
            if abs(units) <= _EPS:
                continue
            price = prices_by_key.get(key, {}).get(curr_day)
            if price is None:
                price = last_trade_price.get(key, 0.0)

            # FX conversion if US equity
            inst_obj = instruments.get(key)
            if inst_obj is not None and inst_obj.currency == "USD":
                fx = fx_series.get(curr_day, 84.0)
                price = price * fx

            day_value += units * price

        # Asymmetric BOD Inflows / EOD Outflows True Daily TWR
        # Denom_d = V_{d-1} + max(0, CF_net) if > 0 else CF_in_gross
        if (prev_value + max(0.0, day_net_inflow)) > _EPS:
            denom = prev_value + max(0.0, day_net_inflow)
        else:
            denom = max(0.0, day_gross_inflow)

        if denom > _EPS and (day_value > _EPS or prev_value > _EPS or day_gross_inflow > _EPS):
            daily_r = (day_value - prev_value - day_net_inflow + day_dividend) / denom
        else:
            # Zero-balance liquidation gap or uninvested period
            daily_r = 0.0

        daily_returns.append(daily_r)
        cum_twr_factor *= 1.0 + daily_r
        gain = day_value - cum_net_invested

        # Cambridge Associates mPME Benchmark Replay Step
        b_price_today = get_b_price_inr(curr_day)
        if b_price_today <= _EPS and last_b_price > _EPS:
            b_price_today = last_b_price
        if b_price_today > _EPS:
            last_b_price = b_price_today

        v_pre = pre_trade_val if pre_trade_val > _EPS else prev_value
        if day_net_inflow > _EPS:
            if b_price_today > _EPS:
                b_units += day_net_inflow / b_price_today
        elif day_net_inflow < -_EPS and v_pre > _EPS:
            # Proportional liquidation on outflow
            withdrawal_fraction = min(1.0, abs(day_net_inflow) / v_pre)
            b_units -= b_units * withdrawal_fraction

        day_benchmark_val = b_units * b_price_today if b_price_today > _EPS else 0.0

        daily_points.append(
            DailyValuationPoint(
                day=curr_day,
                value=day_value,
                net_invested=cum_net_invested,
                gain=gain,
                daily_return=daily_r,
                cumulative_twr=cum_twr_factor - 1.0,
                net_cashflow=day_net_inflow,
                benchmark_value=day_benchmark_val if benchmark_key else None,
                is_invested=(day_value > _EPS),
            )
        )

        prev_value = day_value
        curr_day += one_day

    # Portfolio XIRR Calculation
    xirr_dates: list[date] = []
    xirr_amounts: list[float] = []
    for t in valid_trades:
        if abs(t.amount) > _EPS:
            xirr_dates.append(t.trade_date)
            xirr_amounts.append(-t.amount)
        if t.dividend > _EPS:
            xirr_dates.append(t.trade_date)
            xirr_amounts.append(t.dividend)

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

    # Benchmark XIRR & Alpha
    benchmark_xirr: float | None = None
    benchmark_val_terminal: float | None = None
    alpha: float | None = None

    if benchmark_key and daily_points:
        terminal_pt = daily_points[-1]
        benchmark_val_terminal = terminal_pt.benchmark_value
        if benchmark_val_terminal is not None and benchmark_val_terminal > _EPS:
            b_xirr_dates = [t.trade_date for t in valid_trades if abs(t.amount) > _EPS]
            b_xirr_amounts = [-t.amount for t in valid_trades if abs(t.amount) > _EPS]
            b_xirr_dates.append(end_date)
            b_xirr_amounts.append(benchmark_val_terminal)
            benchmark_xirr = compute_xirr(b_xirr_dates, b_xirr_amounts)
            if portfolio_xirr is not None and benchmark_xirr is not None:
                alpha = portfolio_xirr - benchmark_xirr

    # Monthly Returns Heatmap
    monthly_factors: dict[tuple[int, int], float] = defaultdict(lambda: 1.0)
    for p in daily_points:
        monthly_factors[(p.day.year, p.day.month)] *= 1.0 + p.daily_return

    monthly_returns: list[MonthlyReturn] = [
        MonthlyReturn(year=yr, month=mo, return_pct=factor - 1.0)
        for (yr, mo), factor in sorted(monthly_factors.items())
    ]

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
                inst_obj = instruments.get(k)
                if inst_obj is not None and inst_obj.currency == "USD":
                    pr = pr * fx_series.get(end_date, 84.0)
                m_val += u * pr

        m_dates = [t.trade_date for t in m_trades if abs(t.amount) > _EPS]
        m_amounts = [-t.amount for t in m_trades if abs(t.amount) > _EPS]
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
        benchmark_current_value=benchmark_val_terminal,
        benchmark_xirr=benchmark_xirr,
        alpha=alpha,
        daily_series=daily_points,
        monthly_returns=monthly_returns,
        member_attributions=member_attributions,
        notes=notes,
    )


def compute_period_performances(
    daily_points: list[DailyValuationPoint],
    trades: Sequence[TradeItem],
    as_of: date,
) -> list[PeriodPerformance]:
    """Compute performance breakdown over rolling standard periods."""
    if not daily_points:
        return []

    points_by_date = {p.day: p for p in daily_points}
    earliest_date = daily_points[0].day

    periods_config = [
        ("1M", as_of - timedelta(days=30)),
        ("3M", as_of - timedelta(days=91)),
        ("6M", as_of - timedelta(days=182)),
        ("1Y", date(as_of.year - 1, as_of.month, min(as_of.day, 28))),
        ("3Y", date(as_of.year - 3, as_of.month, min(as_of.day, 28))),
        ("5Y", date(as_of.year - 5, as_of.month, min(as_of.day, 28))),
        ("all", earliest_date),
    ]

    results: list[PeriodPerformance] = []

    for label, target_start in periods_config:
        period_start = max(target_start, earliest_date)
        if period_start > as_of:
            continue

        window_points = [p for p in daily_points if period_start <= p.day <= as_of]
        if not window_points:
            continue

        end_pt = window_points[-1]

        # TWR over window: I_end / I_{start-1} - 1
        # If period_start is earliest_date, base is 1.0
        day_before = period_start - timedelta(days=1)
        prev_pt = points_by_date.get(day_before)
        prev_twr_index = (1.0 + prev_pt.cumulative_twr) if prev_pt else 1.0
        end_twr_index = 1.0 + end_pt.cumulative_twr

        twr_pct = (end_twr_index / prev_twr_index) - 1.0 if prev_twr_index > _EPS else None

        window_days = (as_of - period_start).days
        if window_days >= 365 and twr_pct is not None and twr_pct > -1.0:
            twr_annualized = (1.0 + twr_pct) ** (365.0 / window_days) - 1.0
        else:
            twr_annualized = None

        active_days = sum(1 for p in window_points if p.is_invested)

        # Max Drawdown
        peak = 0.0
        max_dd = 0.0
        for p in window_points:
            if p.value > peak:
                peak = p.value
            elif peak > _EPS:
                dd = (p.value - peak) / peak
                if dd < max_dd:
                    max_dd = dd

        # Volatility
        returns = [p.daily_return for p in window_points]
        if len(returns) > 1:
            mean_r = sum(returns) / len(returns)
            var_r = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
            vol = math.sqrt(var_r * 252.0)
        else:
            vol = None

        # Window XIRR
        xirr_dates: list[date] = []
        xirr_amounts: list[float] = []
        start_val = prev_pt.value if prev_pt else 0.0
        if start_val > _EPS:
            xirr_dates.append(period_start)
            xirr_amounts.append(-start_val)

        for t in trades:
            if period_start <= t.trade_date <= as_of and abs(t.amount) > _EPS:
                xirr_dates.append(t.trade_date)
                xirr_amounts.append(-t.amount)

        if end_pt.value > _EPS:
            xirr_dates.append(as_of)
            xirr_amounts.append(end_pt.value)

        p_xirr = compute_xirr(xirr_dates, xirr_amounts)

        # Window Benchmark XIRR & Alpha
        b_xirr: float | None = None
        alpha_val: float | None = None
        if (
            end_pt.benchmark_value is not None
            and end_pt.benchmark_value > _EPS
            and (start_val > _EPS or len(xirr_dates) > 1)
        ):
            b_dates: list[date] = []
            b_amounts: list[float] = []
            start_b_val = prev_pt.benchmark_value if (prev_pt and prev_pt.benchmark_value) else 0.0
            if start_b_val > _EPS:
                b_dates.append(period_start)
                b_amounts.append(-start_b_val)
            for t in trades:
                if period_start <= t.trade_date <= as_of and abs(t.amount) > _EPS:
                    b_dates.append(t.trade_date)
                    b_amounts.append(-t.amount)
            b_dates.append(as_of)
            b_amounts.append(end_pt.benchmark_value)
            b_xirr = compute_xirr(b_dates, b_amounts)
            if p_xirr is not None and b_xirr is not None:
                alpha_val = p_xirr - b_xirr

        results.append(
            PeriodPerformance(
                period=label,
                start_date=period_start,
                end_date=as_of,
                twr_pct=twr_pct,
                twr_annualized=twr_annualized,
                xirr=p_xirr,
                benchmark_xirr=b_xirr,
                alpha=alpha_val,
                max_drawdown=max_dd,
                volatility_annualized=vol,
                active_days=active_days,
            )
        )

    return results


def persist_portfolio_snapshots(
    session: Session,
    user_id: UUID,
    scope_type: str,
    scope_id: int,
    points: list[PortfolioTimeseriesPoint],
) -> None:
    """Bulk write or replace daily snapshots in portfolio_daily_snapshots."""
    if not points:
        return
    dates = [p.date for p in points]
    min_d, max_d = min(dates), max(dates)
    session.execute(
        delete(PortfolioDailySnapshot).where(
            PortfolioDailySnapshot.user_id == user_id,
            PortfolioDailySnapshot.scope_type == scope_type,
            PortfolioDailySnapshot.scope_id == scope_id,
            PortfolioDailySnapshot.snapshot_date.between(min_d, max_d),
        )
    )
    rows = [
        {
            "user_id": user_id,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "snapshot_date": p.date,
            "portfolio_value_paise": p.portfolio_value_paise,
            "invested_capital_paise": p.invested_capital_paise,
            "net_cashflow_paise": p.net_cashflow_paise,
            "twr_index_micros": p.twr_index_micros,
            "benchmark_value_paise": p.benchmark_value_paise,
            "created_at": clock.naive_utcnow(),
        }
        for p in points
    ]
    session.execute(insert(PortfolioDailySnapshot), rows)


def get_or_compute_portfolio_timeseries(
    session: Session,
    user_id: UUID,
    *,
    scope_type: ScopeTypeStr = "all",
    scope_id: int | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    benchmark_key: str | None = "INDEX:NSEI",
    benchmark_id: int | None = None,
    benchmark_name: str | None = "Nifty 50",
    persist: bool = False,
) -> PortfolioTimeseriesResponse:
    """High-performance compute-on-read timeseries generator with in-memory epoch cache."""
    resolved_scope_id = scope_id or 0
    end_date = to_date or clock.today()
    epoch = _USER_TX_EPOCHS.get(user_id, 0)
    cache_key = (
        user_id,
        scope_type,
        resolved_scope_id,
        from_date,
        end_date,
        benchmark_id,
        benchmark_key,
        epoch,
    )

    if cache_key in _TIMESERIES_CACHE:
        cached_time, resp = _TIMESERIES_CACHE[cache_key]
        if persist and resp.points:
            persist_portfolio_snapshots(
                session,
                user_id,
                scope_type,
                resolved_scope_id,
                resp.points,
            )
        return resp

    trades = load_user_trades(
        session,
        user_id,
        scope_type=scope_type,
        scope_id=resolved_scope_id,
    )

    report = calculate_portfolio_report(
        session,
        trades,
        end=end_date,
        benchmark_key=benchmark_key,
        benchmark_id=benchmark_id,
    )

    if not report.daily_series:
        resp = PortfolioTimeseriesResponse(
            scope=scope_type,
            account_id=scope_id,
            start_date=end_date,
            end_date=end_date,
            benchmark_id=benchmark_id,
            benchmark_name=benchmark_name,
            points=[],
            periods=[],
        )
        _TIMESERIES_CACHE[cache_key] = (clock.naive_utcnow(), resp)
        return resp

    # Convert report daily_series to API points
    all_points: list[PortfolioTimeseriesPoint] = []
    for p in report.daily_series:
        # twr_index_micros: base 100.0 * 10^4 = 1,000,000
        twr_micros = int(round(1_000_000 * (1.0 + p.cumulative_twr)))
        bench_paise = int(round(p.benchmark_value * 100)) if p.benchmark_value is not None else None
        all_points.append(
            PortfolioTimeseriesPoint(
                date=p.day,
                portfolio_value_paise=int(round(p.value * 100)),
                invested_capital_paise=int(round(p.net_invested * 100)),
                twr_index_micros=twr_micros,
                benchmark_value_paise=bench_paise,
                daily_return=p.daily_return,
                net_cashflow_paise=int(round(p.net_cashflow * 100)),
                is_invested=p.is_invested,
            )
        )

    if persist:
        persist_portfolio_snapshots(
            session,
            user_id,
            scope_type,
            resolved_scope_id,
            all_points,
        )

    # Slice for response if from_date specified
    filtered_points = [pt for pt in all_points if pt.date >= from_date] if from_date else all_points

    periods = compute_period_performances(report.daily_series, trades, end_date)

    response = PortfolioTimeseriesResponse(
        scope=scope_type,
        account_id=scope_id,
        start_date=all_points[0].date if all_points else end_date,
        end_date=end_date,
        benchmark_id=benchmark_id,
        benchmark_name=benchmark_name,
        points=filtered_points,
        periods=periods,
    )

    _TIMESERIES_CACHE[cache_key] = (clock.naive_utcnow(), response)
    return response
