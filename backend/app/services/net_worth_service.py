"""Net Worth Timeline & Cashflow Waterfall engine (PRD §F8 View 4 & Wealth Analytics).

Provides:
1. compute_net_worth_timeline:
   Consolidates three distinct historical balance feeds:
   - Liquid cash & bank accounts (opening balances + non-investment cashflows,
     overdrafts reduce net worth)
   - Investment portfolio daily valuation from portfolio_daily_snapshots & forward-sweep
   - Peer debts (Khatabook ADR-0017: net receivables add to assets,
     net payables count as liabilities)
   - Credit cards strictly excluded from net worth liability clamp (PRD §F8 View 4)
   - Asset category breakdown: bank_cash, indian_equity, indian_mf, us_equity, debt_fd, gold,
     peer_receivables, peer_payables
   - Decimation: daily for <= 1Y, weekly/monthly for > 1Y, preserving exact closing values.
2. compute_net_worth_summary:
   Headline net worth, 30-day delta (₹ and %), 1-year delta, and asset vs liability breakdown.
3. compute_cashflow_waterfall:
   Calendar or financial year waterfall:
   Gross Income -> Living Expenses -> Peer Net -> Investment Transfers -> Liquid Surplus.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, aliased

from app.core import clock
from app.core.fiscal_year import fy_bounds
from app.core.log_config import get_logger
from app.models.account import Account
from app.models.category import Category
from app.models.instrument import Instrument
from app.models.investment_transaction import InvestmentTransaction
from app.models.market_pricing import MarketInstrument
from app.models.peer_entry import PeerEntry
from app.models.portfolio_daily_snapshot import PortfolioDailySnapshot
from app.models.transaction import Transaction
from app.schemas.net_worth import (
    AssetCategoryBreakdown,
    CashflowWaterfallResponse,
    NetWorthSummaryResponse,
    NetWorthTimelinePoint,
    NetWorthTimelineResponse,
    TimelineIntervalStr,
    TimelineRangeStr,
    WaterfallCategoryItem,
    WaterfallStep,
    YearTypeStr,
)
from app.services.category_service import INVESTMENT_TRANSFER_CATEGORY_NAME
from app.services.market_pricing import filled_series, get_instrument
from app.services.portfolio_analytics_service import (
    instrument_to_price_key,
    load_user_trades,
)

logger = get_logger(__name__)

_EPS = 1e-9


def _map_asset_class_category(asset_class: str) -> str:
    """Map Instrument.asset_class to one of the 5 investment categories."""
    if asset_class == "indian_equity":
        return "indian_equity"
    if asset_class == "indian_mf":
        return "indian_mf"
    if asset_class in ("us_equity", "us_etf"):
        return "us_equity"
    if asset_class in ("fd", "bond", "nps"):
        return "debt_fd"
    if asset_class in ("gold", "other"):
        return "gold"
    return "gold"


@dataclass(slots=True)
class _DailyInvestments:
    indian_equity_paise: int = 0
    indian_mf_paise: int = 0
    us_equity_paise: int = 0
    debt_fd_paise: int = 0
    gold_paise: int = 0

    @property
    def total_paise(self) -> int:
        return (
            self.indian_equity_paise
            + self.indian_mf_paise
            + self.us_equity_paise
            + self.debt_fd_paise
            + self.gold_paise
        )


def _compute_investment_forward_sweep(
    session: Session,
    user_id: UUID,
    end_date: date,
) -> tuple[dict[date, _DailyInvestments], dict[date, int]]:
    """Run an investment forward sweep and return daily asset-class breakdown and total values.

    Returns:
    - breakdown_by_date: dict[date, _DailyInvestments]
    - snapshot_totals_by_date: dict[date, int] (from portfolio_daily_snapshots if any)
    """
    # 1. Read existing snapshots if already persisted in portfolio_daily_snapshots
    snapshot_stmt = (
        select(PortfolioDailySnapshot.snapshot_date, PortfolioDailySnapshot.portfolio_value_paise)
        .where(
            PortfolioDailySnapshot.user_id == user_id,
            PortfolioDailySnapshot.scope_type == "all",
            PortfolioDailySnapshot.scope_id == 0,
            PortfolioDailySnapshot.snapshot_date <= end_date,
        )
        .order_by(PortfolioDailySnapshot.snapshot_date.asc())
    )
    snapshot_totals: dict[date, int] = {
        row[0]: int(row[1]) for row in session.execute(snapshot_stmt).all()
    }

    # 2. Load user instruments to map price_key -> asset_class category
    inst_stmt = select(Instrument).where(Instrument.user_id == user_id)
    user_insts = session.scalars(inst_stmt).all()
    if not user_insts:
        return {}, snapshot_totals

    key_to_cat: dict[str, str] = {}
    for inst in user_insts:
        key = instrument_to_price_key(inst)
        key_to_cat[key] = _map_asset_class_category(inst.asset_class)

    trades = load_user_trades(session, user_id, scope_type="all", scope_id=0)
    valid_trades = [t for t in trades if t.trade_date <= end_date]
    if not valid_trades:
        return {}, snapshot_totals

    start_date = min(t.trade_date for t in valid_trades)
    distinct_keys = sorted({t.price_key for t in valid_trades})
    all_keys = set(distinct_keys)
    all_keys.add("FX:USDINR")

    instruments: dict[str, MarketInstrument] = {}
    prices_by_key: dict[str, dict[date, float]] = {}

    for key in all_keys:
        inst_market = get_instrument(session, key)
        if inst_market is None:
            continue
        instruments[key] = inst_market
        series = filled_series(session, inst_market.id, start_date, end_date)
        prices_by_key[key] = {p.day: p.nav_micros / 10_000.0 for p in series}

    # Fallback to trade price
    last_trade_price: dict[str, float] = {}
    for t in sorted(valid_trades, key=lambda x: x.trade_date):
        if abs(t.units) > _EPS:
            last_trade_price[t.price_key] = abs(t.amount / t.units)

    fx_series = prices_by_key.get("FX:USDINR", {})

    trades_by_date: dict[date, list] = defaultdict(list)
    for t in valid_trades:
        trades_by_date[t.trade_date].append(t)

    current_units: dict[str, float] = defaultdict(float)
    breakdown_by_date: dict[date, _DailyInvestments] = {}

    curr_day = start_date
    one_day = timedelta(days=1)

    while curr_day <= end_date:
        if curr_day in trades_by_date:
            for t in trades_by_date[curr_day]:
                current_units[t.price_key] += t.units

        day_breakdown = _DailyInvestments()
        for key, units in current_units.items():
            if abs(units) <= _EPS:
                continue
            pr = prices_by_key.get(key, {}).get(curr_day)
            if pr is None:
                pr = last_trade_price.get(key, 0.0)

            inst_obj = instruments.get(key)
            if inst_obj is not None and inst_obj.currency == "USD":
                fx = fx_series.get(curr_day, 84.0)
                pr = pr * fx

            val_paise = int(round(units * pr * 100))
            cat = key_to_cat.get(key, "gold")
            if cat == "indian_equity":
                day_breakdown.indian_equity_paise += val_paise
            elif cat == "indian_mf":
                day_breakdown.indian_mf_paise += val_paise
            elif cat == "us_equity":
                day_breakdown.us_equity_paise += val_paise
            elif cat == "debt_fd":
                day_breakdown.debt_fd_paise += val_paise
            elif cat == "gold":
                day_breakdown.gold_paise += val_paise

        # Reconcile with snapshot total if available and breakdown has entries
        if curr_day in snapshot_totals:
            snap_total = snapshot_totals[curr_day]
            calc_total = day_breakdown.total_paise
            if calc_total != snap_total and calc_total > 0:
                # Slight market pricing vs snapshot delta: scale proportionally or adjust dominant
                scale = snap_total / calc_total
                day_breakdown.indian_equity_paise = int(
                    round(day_breakdown.indian_equity_paise * scale)
                )
                day_breakdown.indian_mf_paise = int(round(day_breakdown.indian_mf_paise * scale))
                day_breakdown.us_equity_paise = int(round(day_breakdown.us_equity_paise * scale))
                day_breakdown.debt_fd_paise = int(round(day_breakdown.debt_fd_paise * scale))
                day_breakdown.gold_paise = int(round(day_breakdown.gold_paise * scale))

        breakdown_by_date[curr_day] = day_breakdown
        curr_day += one_day

    return breakdown_by_date, snapshot_totals


def compute_net_worth_timeline(
    session: Session,
    *,
    user_id: UUID,
    range_str: TimelineRangeStr = "1Y",
    as_of: date | None = None,
) -> NetWorthTimelineResponse:
    """Compute the historical net worth timeline with asset-category breakdown and decimation.

    Consolidates:
    1. Liquid Cash & Bank Accounts (opening balances + confirmed non-CC txns)
    2. Investment Portfolio (daily forward-sweep across asset categories)
    3. Peer Debts (Khatabook ADR-0017 dynamic contact balances)
    4. Credit Cards: Strictly excluded from net worth liability clamp.
    """
    end_date = as_of or clock.today()

    # 1. Determine start_date based on range_str
    if range_str == "1M":
        start_date = end_date - timedelta(days=30)
    elif range_str == "6M":
        start_date = end_date - timedelta(days=180)
    elif range_str == "1Y":
        start_date = end_date - timedelta(days=365)
    elif range_str == "3Y":
        start_date = end_date - timedelta(days=365 * 3)
    elif range_str == "5Y":
        start_date = end_date - timedelta(days=365 * 5)
    else:  # ALL
        # Earliest date across bank accounts, investment trades, and peer entries
        dates: list[date] = []
        earliest_txn = session.scalar(
            select(Transaction.date)
            .join(Account, and_(Account.id == Transaction.account_id, Account.user_id == user_id))
            .where(
                Transaction.user_id == user_id,
                Account.type.in_(("bank", "cash")),
                Transaction.confirmed_at.is_not(None),
            )
            .order_by(Transaction.date.asc())
            .limit(1)
        )
        if earliest_txn:
            dates.append(earliest_txn)

        earliest_inv = session.scalar(
            select(InvestmentTransaction.date)
            .where(InvestmentTransaction.user_id == user_id)
            .order_by(InvestmentTransaction.date.asc())
            .limit(1)
        )
        if earliest_inv:
            dates.append(earliest_inv)

        earliest_peer = session.scalar(
            select(PeerEntry.date)
            .where(PeerEntry.user_id == user_id)
            .order_by(PeerEntry.date.asc())
            .limit(1)
        )
        if earliest_peer:
            dates.append(earliest_peer)

        earliest_snap = session.scalar(
            select(PortfolioDailySnapshot.snapshot_date)
            .where(PortfolioDailySnapshot.user_id == user_id)
            .order_by(PortfolioDailySnapshot.snapshot_date.asc())
            .limit(1)
        )
        if earliest_snap:
            dates.append(earliest_snap)

        start_date = min(dates) if dates else (end_date - timedelta(days=30))

    if start_date > end_date:
        start_date = end_date

    # 2. Bank & Cash Accounts cumulative balance feed
    bank_cash_accounts = session.scalars(
        select(Account).where(
            Account.user_id == user_id,
            Account.type.in_(("bank", "cash")),
        )
    ).all()
    initial_bank_cash = sum(a.opening_balance_paise for a in bank_cash_accounts)

    txn_stmt = (
        select(Transaction.date, Transaction.amount_paise)
        .join(Account, and_(Account.id == Transaction.account_id, Account.user_id == user_id))
        .where(
            Transaction.user_id == user_id,
            Account.type.in_(("bank", "cash")),
            Transaction.confirmed_at.is_not(None),
            Transaction.date <= end_date,
        )
        .order_by(Transaction.date.asc())
    )
    all_bank_txns = session.execute(txn_stmt).all()

    bank_deltas_by_date: dict[date, int] = defaultdict(int)
    bank_cash_prior_to_start = initial_bank_cash
    for txn_date, amount_paise in all_bank_txns:
        if txn_date < start_date:
            bank_cash_prior_to_start += int(amount_paise)
        else:
            bank_deltas_by_date[txn_date] += int(amount_paise)

    # 3. Peer Debts (Khatabook) feed
    peer_stmt = (
        select(PeerEntry.contact_id, PeerEntry.date, PeerEntry.amount_paise, PeerEntry.direction)
        .where(PeerEntry.user_id == user_id, PeerEntry.date <= end_date)
        .order_by(PeerEntry.date.asc())
    )
    all_peer_entries = session.execute(peer_stmt).all()

    contact_balances: dict[int, int] = defaultdict(int)
    peer_entries_by_date: dict[date, list[tuple[int, int, str]]] = defaultdict(list)

    for contact_id, entry_date, amount_paise, direction in all_peer_entries:
        amt = int(amount_paise)
        if entry_date < start_date:
            if direction == "you_gave":
                contact_balances[contact_id] += amt
            else:
                contact_balances[contact_id] -= amt
        else:
            peer_entries_by_date[entry_date].append((contact_id, amt, direction))

    # 4. Investment Portfolio feed
    inv_breakdown_by_date, snap_totals = _compute_investment_forward_sweep(
        session, user_id, end_date
    )

    # Live holdings anchor for today and future dates:
    # Eliminates pricing divergence between live Instrument.current_nav and historical quotes.
    live_today_breakdown: _DailyInvestments | None = None
    today_dt = clock.today()
    if end_date >= today_dt:
        try:
            from app.services.fx_service import latest_rate
            from app.services.holdings_service import compute_holdings

            usd_inr = latest_rate(session)
            live_holdings = compute_holdings(session, user_id=user_id, usd_inr_rate=usd_inr)
            b = _DailyInvestments()
            for h in live_holdings:
                val = h.current_value_inr_paise or 0
                cat = _map_asset_class_category(h.asset_class)
                if cat == "indian_equity":
                    b.indian_equity_paise += val
                elif cat == "indian_mf":
                    b.indian_mf_paise += val
                elif cat == "us_equity":
                    b.us_equity_paise += val
                elif cat == "debt_fd":
                    b.debt_fd_paise += val
                elif cat == "gold":
                    b.gold_paise += val
            if b.total_paise > 0:
                live_today_breakdown = b
        except Exception:
            live_today_breakdown = None

    # 5. Daily forward sweep from start_date to end_date
    curr_bank_cash = bank_cash_prior_to_start
    daily_points: list[NetWorthTimelinePoint] = []
    curr_day = start_date
    one_day = timedelta(days=1)

    while curr_day <= end_date:
        # Update bank cash
        if curr_day in bank_deltas_by_date:
            curr_bank_cash += bank_deltas_by_date[curr_day]

        # Update peer debts
        if curr_day in peer_entries_by_date:
            for c_id, amt, direction in peer_entries_by_date[curr_day]:
                if direction == "you_gave":
                    contact_balances[c_id] += amt
                else:
                    contact_balances[c_id] -= amt

        peer_rec = sum(b for b in contact_balances.values() if b > 0)
        peer_pay = sum(-b for b in contact_balances.values() if b < 0)

        # Investment breakdown: for today and forward, prioritize authoritative live NAVs
        if curr_day >= today_dt and live_today_breakdown is not None:
            inv_day = live_today_breakdown
        else:
            inv_day = inv_breakdown_by_date.get(curr_day, _DailyInvestments())
        ie_paise = inv_day.indian_equity_paise
        imf_paise = inv_day.indian_mf_paise
        ue_paise = inv_day.us_equity_paise
        df_paise = inv_day.debt_fd_paise
        gold_paise = inv_day.gold_paise

        inv_total = ie_paise + imf_paise + ue_paise + df_paise + gold_paise
        if inv_total == 0 and curr_day in snap_totals:
            inv_total = snap_totals[curr_day]
            imf_paise = inv_total

        # Liquid cash overdraft handling: overdrafts count as liabilities
        liquid_asset = max(0, curr_bank_cash)
        liquid_liability = max(0, -curr_bank_cash)

        assets = liquid_asset + inv_total + peer_rec
        liabilities = liquid_liability + peer_pay
        net_worth = assets - liabilities

        daily_points.append(
            NetWorthTimelinePoint(
                date=curr_day,
                net_worth_paise=net_worth,
                assets_paise=assets,
                liabilities_paise=liabilities,
                bank_cash_paise=curr_bank_cash,
                indian_equity_paise=ie_paise,
                indian_mf_paise=imf_paise,
                us_equity_paise=ue_paise,
                debt_fd_paise=df_paise,
                gold_paise=gold_paise,
                peer_receivables_paise=peer_rec,
                peer_payables_paise=peer_pay,
            )
        )
        curr_day += one_day

    # 6. Decimation interval determination
    total_days = (end_date - start_date).days
    interval: TimelineIntervalStr = "daily"

    if range_str == "3Y":
        interval = "weekly"
    elif range_str in ("5Y", "ALL"):
        if total_days > 365 * 3:
            interval = "monthly"
        elif total_days > 365:
            interval = "weekly"
        else:
            interval = "daily"

    # 7. Apply interval decimation (preserving exact end-of-period closing values)
    if interval == "daily" or len(daily_points) <= 1:
        selected_points = daily_points
    elif interval == "weekly":
        # Group by ISO year and week, taking the last point in each week
        weeks: dict[tuple[int, int], NetWorthTimelinePoint] = {}
        for p in daily_points:
            iso_year, iso_week, _ = p.date.isocalendar()
            weeks[(iso_year, iso_week)] = p
        selected_points = list(weeks.values())
        # Ensure latest point (today) is explicitly present
        if daily_points and (
            not selected_points or selected_points[-1].date != daily_points[-1].date
        ):
            selected_points.append(daily_points[-1])
    else:  # monthly
        # Group by year and month, taking the last point in each month
        months: dict[tuple[int, int], NetWorthTimelinePoint] = {}
        for p in daily_points:
            months[(p.date.year, p.date.month)] = p
        selected_points = list(months.values())
        # Ensure latest point (today) is explicitly present
        if daily_points and (
            not selected_points or selected_points[-1].date != daily_points[-1].date
        ):
            selected_points.append(daily_points[-1])

    # Sort ascending by date
    selected_points.sort(key=lambda x: x.date)

    return NetWorthTimelineResponse(
        range=range_str,
        interval=interval,
        start_date=start_date,
        end_date=end_date,
        points=selected_points,
    )


def compute_net_worth_summary(
    session: Session,
    *,
    user_id: UUID,
    as_of: date | None = None,
) -> NetWorthSummaryResponse:
    """Compute headline scalar net worth, 30d/1y deltas, and asset vs liability breakdown."""
    today = as_of or clock.today()

    # Timeline spanning 1 year and 30 days back to get today, 30d ago, and 1y ago closing points
    timeline = compute_net_worth_timeline(
        session,
        user_id=user_id,
        range_str="ALL",
        as_of=today,
    )
    points = timeline.points
    points_by_date = {p.date: p for p in points}

    today_pt = points_by_date.get(today) or (points[-1] if points else None)

    if today_pt is None:
        empty_breakdown = AssetCategoryBreakdown(
            bank_cash_paise=0,
            indian_equity_paise=0,
            indian_mf_paise=0,
            us_equity_paise=0,
            debt_fd_paise=0,
            gold_paise=0,
            peer_receivables_paise=0,
            peer_payables_paise=0,
        )
        return NetWorthSummaryResponse(
            as_of=today,
            net_worth_paise=0,
            assets_paise=0,
            liabilities_paise=0,
            delta_30d_paise=0,
            delta_30d_pct=None,
            delta_1y_paise=0,
            delta_1y_pct=None,
            breakdown=empty_breakdown,
        )

    # Find point closest on or before 30 days ago
    target_30d = today - timedelta(days=30)
    pt_30d = points_by_date.get(target_30d)
    if pt_30d is None:
        eligible_30d = [p for p in points if p.date <= target_30d]
        pt_30d = eligible_30d[-1] if eligible_30d else (points[0] if points else None)

    # Find point closest on or before 1 year ago
    target_1y = today - timedelta(days=365)
    pt_1y = points_by_date.get(target_1y)
    if pt_1y is None:
        eligible_1y = [p for p in points if p.date <= target_1y]
        pt_1y = eligible_1y[-1] if eligible_1y else (points[0] if points else None)

    nw_today = today_pt.net_worth_paise
    nw_30d = pt_30d.net_worth_paise if pt_30d else nw_today
    nw_1y = pt_1y.net_worth_paise if pt_1y else nw_today

    delta_30d_paise = nw_today - nw_30d
    delta_30d_pct: float | None = None
    if nw_30d > 0:
        delta_30d_pct = round((delta_30d_paise / nw_30d) * 100, 2)

    delta_1y_paise = nw_today - nw_1y
    delta_1y_pct: float | None = None
    if nw_1y > 0:
        delta_1y_pct = round((delta_1y_paise / nw_1y) * 100, 2)

    breakdown = AssetCategoryBreakdown(
        bank_cash_paise=today_pt.bank_cash_paise,
        indian_equity_paise=today_pt.indian_equity_paise,
        indian_mf_paise=today_pt.indian_mf_paise,
        us_equity_paise=today_pt.us_equity_paise,
        debt_fd_paise=today_pt.debt_fd_paise,
        gold_paise=today_pt.gold_paise,
        peer_receivables_paise=today_pt.peer_receivables_paise,
        peer_payables_paise=today_pt.peer_payables_paise,
    )

    return NetWorthSummaryResponse(
        as_of=today,
        net_worth_paise=nw_today,
        assets_paise=today_pt.assets_paise,
        liabilities_paise=today_pt.liabilities_paise,
        delta_30d_paise=delta_30d_paise,
        delta_30d_pct=delta_30d_pct,
        delta_1y_paise=delta_1y_paise,
        delta_1y_pct=delta_1y_pct,
        breakdown=breakdown,
    )


def compute_cashflow_waterfall(
    session: Session,
    *,
    user_id: UUID,
    year: int,
    year_type: YearTypeStr = "calendar",
) -> CashflowWaterfallResponse:
    """Compute annual inflow-to-wealth cashflow waterfall decomposition.

    Steps:
    Gross Income -> Living Expenses -> Peer Net -> Investment Transfers -> Liquid Surplus.
    """
    if year_type == "calendar":
        start_date = date(year, 1, 1)
        end_date = date(year, 12, 31)
    else:  # fiscal
        start_date, end_date = fy_bounds(year)

    # 1. Gross Income (signed sum of income transactions, reversals net down)
    income_stmt = select(Transaction.amount_paise).where(
        Transaction.user_id == user_id,
        Transaction.transaction_type == "income",
        Transaction.confirmed_at.is_not(None),
        Transaction.date.between(start_date, end_date),
    )
    gross_income_paise = sum(session.scalars(income_stmt).all() or [0])

    # 2. Investment transfer category identification
    inv_transfer_cat_id = session.scalar(
        select(Category.id).where(
            Category.user_id == user_id,
            Category.name == INVESTMENT_TRANSFER_CATEGORY_NAME,
        )
    )

    # 3. Living Expenses (signed sum of spend transactions, excluding investment transfers)
    spend_cat = aliased(Category)
    spend_parent = aliased(Category)

    spend_stmt = (
        select(
            Transaction.amount_paise,
            Transaction.category_id,
            spend_cat.name.label("cat_name"),
            spend_cat.color.label("cat_color"),
            spend_cat.parent_id.label("parent_id"),
            spend_parent.name.label("parent_name"),
            spend_parent.color.label("parent_color"),
        )
        .outerjoin(
            spend_cat,
            and_(spend_cat.id == Transaction.category_id, spend_cat.user_id == user_id),
        )
        .outerjoin(
            spend_parent,
            and_(spend_parent.id == spend_cat.parent_id, spend_parent.user_id == user_id),
        )
        .where(
            Transaction.user_id == user_id,
            Transaction.transaction_type == "spend",
            Transaction.confirmed_at.is_not(None),
            Transaction.date.between(start_date, end_date),
        )
    )
    if inv_transfer_cat_id is not None:
        spend_stmt = spend_stmt.where(
            or_(
                Transaction.category_id.is_(None),
                Transaction.category_id != inv_transfer_cat_id,
            )
        )

    spend_rows = session.execute(spend_stmt).all()

    # Roll up expenses to parent categories
    parent_spend_paise: dict[str, int] = defaultdict(int)
    parent_colors: dict[str, str | None] = {}
    parent_ids: dict[str, int | None] = {}

    for row in spend_rows:
        amt = int(row.amount_paise)
        p_name = row.parent_name or row.cat_name or "Uncategorized"
        p_color = row.parent_color or row.cat_color
        p_id = row.parent_id or row.category_id

        parent_spend_paise[p_name] += amt
        if p_name not in parent_colors or parent_colors[p_name] is None:
            parent_colors[p_name] = p_color
        if p_name not in parent_ids:
            parent_ids[p_name] = p_id

    total_living_expenses_paise = sum(parent_spend_paise.values())

    # Sort parent categories by largest spend first (most negative)
    sorted_parents = sorted(parent_spend_paise.items(), key=lambda x: x[1])
    top_categories = sorted_parents[:5]
    other_categories = sorted_parents[5:]

    expense_category_items: list[WaterfallCategoryItem] = []
    for name, amt in top_categories:
        expense_category_items.append(
            WaterfallCategoryItem(
                category_id=parent_ids.get(name),
                name=name,
                amount_paise=amt,
                display_color=parent_colors.get(name),
            )
        )

    if other_categories:
        other_amt = sum(amt for _, amt in other_categories)
        expense_category_items.append(
            WaterfallCategoryItem(
                category_id=None,
                name="Other Living Expenses",
                amount_paise=other_amt,
                display_color="#64748b",
            )
        )

    # 4. Peer Net Lending / Repayments
    peer_stmt = select(PeerEntry.amount_paise, PeerEntry.direction).where(
        PeerEntry.user_id == user_id,
        PeerEntry.date.between(start_date, end_date),
    )
    peer_entries = session.execute(peer_stmt).all()
    total_you_gave = sum(int(amt) for amt, direction in peer_entries if direction == "you_gave")
    total_you_got = sum(int(amt) for amt, direction in peer_entries if direction == "you_got")
    peer_net_paise = total_you_got - total_you_gave  # Positive if net cash came in

    # 5. Net Investment Transfers
    # From system:investment_transfer category if recorded
    inv_transfer_spend = 0
    if inv_transfer_cat_id is not None:
        inv_stmt = select(Transaction.amount_paise).where(
            Transaction.user_id == user_id,
            Transaction.category_id == inv_transfer_cat_id,
            Transaction.confirmed_at.is_not(None),
            Transaction.date.between(start_date, end_date),
        )
        inv_transfer_spend = sum(session.scalars(inv_stmt).all() or [0])

    if inv_transfer_spend != 0:
        investment_transfers_paise = inv_transfer_spend
    else:
        # Fallback to net trades cash deployed from investment_transactions
        trades = load_user_trades(session, user_id, scope_type="all", scope_id=0)
        in_window_trades = [t for t in trades if start_date <= t.trade_date <= end_date]
        # Net deployed into investments: cash paid in (t.amount > 0 is outflow to investment)
        net_deployed_paise = int(round(sum(t.amount for t in in_window_trades) * 100))
        investment_transfers_paise = -net_deployed_paise

    # 6. Net Liquid Surplus
    net_liquid_surplus_paise = (
        gross_income_paise
        + total_living_expenses_paise
        + peer_net_paise
        + investment_transfers_paise
    )

    # 7. Construct discrete waterfall steps
    steps: list[WaterfallStep] = []
    idx = 1
    running_level = gross_income_paise

    steps.append(
        WaterfallStep(
            step_index=idx,
            label="Gross Income",
            amount_paise=gross_income_paise,
            cumulative_paise=running_level,
            kind="income",
            display_color="#10b981",
        )
    )

    for item in expense_category_items:
        idx += 1
        running_level += item.amount_paise
        steps.append(
            WaterfallStep(
                step_index=idx,
                label=item.name,
                amount_paise=item.amount_paise,
                cumulative_paise=running_level,
                kind="expense",
                display_color=item.display_color or "#ef4444",
            )
        )

    if peer_net_paise != 0:
        idx += 1
        running_level += peer_net_paise
        steps.append(
            WaterfallStep(
                step_index=idx,
                label="Peer Lending / Dues",
                amount_paise=peer_net_paise,
                cumulative_paise=running_level,
                kind="peer",
                display_color="#8b5cf6" if peer_net_paise > 0 else "#f59e0b",
            )
        )

    if investment_transfers_paise != 0:
        idx += 1
        running_level += investment_transfers_paise
        steps.append(
            WaterfallStep(
                step_index=idx,
                label="Investment Additions",
                amount_paise=investment_transfers_paise,
                cumulative_paise=running_level,
                kind="investment",
                display_color="#3b82f6",
            )
        )

    idx += 1
    steps.append(
        WaterfallStep(
            step_index=idx,
            label="Net Liquid Surplus",
            amount_paise=net_liquid_surplus_paise,
            cumulative_paise=net_liquid_surplus_paise,
            kind="surplus",
            display_color="#06b6d4",
        )
    )

    return CashflowWaterfallResponse(
        year=year,
        year_type=year_type,
        start_date=start_date,
        end_date=end_date,
        gross_income_paise=gross_income_paise,
        total_living_expenses_paise=total_living_expenses_paise,
        expense_categories=expense_category_items,
        peer_net_paise=peer_net_paise,
        investment_transfers_paise=investment_transfers_paise,
        net_liquid_surplus_paise=net_liquid_surplus_paise,
        steps=steps,
    )
