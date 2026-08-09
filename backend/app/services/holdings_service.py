"""Holdings read-model — current positions computed from investment_transactions.

There is **no** ``holdings`` table (PRD §Data model: net worth / positions are
query-time aggregates, no snapshot cache). :func:`compute_holdings` replays every
investment transaction for a user through a per-instrument FIFO lot queue and
returns the live position per instrument.

FIFO (PRD §F7): the first lot bought is the first sold. Lots are consumed in
``(date, id)`` order — the ``id``-ascending tie-break for same-date lots is the
locked decision (mirrors the index ``ix_investment_transactions_user_instrument_date``).
On a partial sell, the touched lot releases a *proportional* share of its remaining
cost basis, so ``invested_native_paise`` is the acquisition cost of the units still
held.

This handles ``buy`` / ``sip`` / ``sell`` / ``dividend`` / ``bonus`` plus the
``switch`` legs: ``switch_out`` releases units FIFO like a ``sell`` and ``switch_in``
acquires them like a ``buy`` (PRD §F7 248-251). ``split`` stays a deferred no-op. No
importer currently produces ``split`` / ``switch_*`` rows (the CSV importer rejects
them; manual entry rejects them too), so those branches are unreachable today — kept
for when a corporate-action source lands. All money math is integer paise; ``units``
stays ``Decimal``. No float.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.log_config import get_logger
from app.models import Instrument, InvestmentTransaction, InvestmentTxnTypeStr
from app.models.account import CurrencyStr
from app.models.instrument import AssetClassStr
from app.services.fx_math import to_inr_paise

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Holding:
    """One current position. ``*_native_paise`` are in the instrument's own currency
    (the per-row display values); ``*_inr_paise`` are the home-currency rollup values.

    ``invested_inr_paise`` is the sum of each surviving FIFO lot's native cost converted
    at *that lot's* acquisition-date ``fx_rate_to_inr`` (historical clock) — NOT
    ``invested_native × today's rate``, which would smear FX moves into the cost basis.
    ``current_value_inr_paise`` is the native current value at the as-of rate (today's
    clock); it is ``None`` for a USD holding when no FX rate is cached (FX-unavailable —
    surfaced, never silently treated as 1:1). For an INR holding the rate is exactly 1, so
    every ``*_inr_paise`` equals its ``*_native_paise`` (the backward-compat no-op).

    ``nav_staleness_days`` is this row's valuation age — how old the price behind
    ``current_value`` is (:class:`app.models.instrument.Instrument`: ``nav_updated_at`` is
    a valuation date on every write path). ``None`` when the holding has no NAV, or when
    the caller supplied no ``as_of``. It is computed here rather than shipped as a raw
    stamp on purpose: SQLite hands the column back naive, so it serializes with no
    timezone suffix, and ``new Date("…T20:00:00")`` in a browser parses that as LOCAL
    time. A client deriving the age itself would re-introduce exactly the off-by-one this
    milestone is fixing.
    """

    instrument_id: int
    symbol: str
    name: str
    asset_class: AssetClassStr
    currency: CurrencyStr
    net_units: Decimal
    avg_cost_native: Decimal
    invested_native_paise: int
    current_nav: Decimal | None
    current_value_native_paise: int | None
    unrealized_pnl_native_paise: int | None
    invested_inr_paise: int
    current_value_inr_paise: int | None
    unrealized_pnl_inr_paise: int | None
    nav_staleness_days: int | None


@dataclass(frozen=True, slots=True)
class HoldingsValueRollup:
    """Portfolio tiles rolled up from :func:`compute_holdings` output — **INR paise**.

    Value / invested / unrealized and ``holdings_count`` cover the **NAV-bearing**
    set only (``current_value_inr_paise is not None``); a holding with no NAV is tallied in
    ``null_nav_count``, and a USD holding that *is* priced but has no cached FX rate (so it
    can't be rolled up to INR) is tallied separately in ``fx_unavailable_count`` — it is
    excluded from the totals *and from the XIRR/alpha cashflows*, so the count must travel
    to the response as an honesty flag rather than silently shrinking the number.
    """

    current_value_paise: int
    invested_paise: int
    unrealized_pnl_paise: int
    holdings_count: int  # NAV-bearing
    null_nav_count: int
    fx_unavailable_count: int


def max_staleness_days(stamps: Iterable[datetime | None], *, as_of: date) -> int | None:
    """Oldest valuation age in days across ``stamps``, or ``None`` if none are set.

    **The population is the caller's to choose, and the callers choose differently** —
    that is the whole reason this takes an iterable instead of running its own query.
    ``performance_service`` folds it over the *sourced holdings* (NAV-bearing, currently
    held, FX-priceable); ``nav_snapshot_service`` folds it over the *instrument
    catalogue*, which includes fully-exited positions. Both numbers are correct answers
    to different questions, and both used to be called ``nav_staleness_days``, so the
    same user at the same instant could be told 1 and 200. Naming them apart is half the
    fix; sharing one expression is the other half, so a change to the arithmetic cannot
    land on one and not the other.

    ``.date()`` is the mandatory ``datetime`` → ``date`` narrowing (``as_of`` is a
    ``date``), not a naive/aware scar — ADR-0001 rule 5 and :mod:`app.core.clock` own that.

    Lives here rather than in ``nav_snapshot_service`` because this module imports only
    models and ``fx_math``: routing it through the AMFI/Yahoo module would drag ``httpx``
    and ``app.parsers`` into ``performance_service``'s import graph for three lines of
    arithmetic.
    """
    ages = [(as_of - s.date()).days for s in stamps if s is not None]
    return max(ages) if ages else None


@dataclass(slots=True)
class _Lot:
    """A mutable open lot: units still held, their remaining cost basis, and the
    acquisition-date FX rate (rides with the lot through FIFO consumption so a surviving
    lot keeps the rate it was bought at — load-bearing for ``invested_inr_paise``)."""

    units: Decimal
    cost_paise: int
    fx_rate_to_inr: Decimal


def compute_holdings(
    session: Session,
    *,
    user_id: UUID,
    usd_inr_rate: Decimal | None = None,
    as_of: date | None = None,
) -> list[Holding]:
    """Return current positions (``net_units > 0``) for ``user_id``, symbol-sorted.

    Two queries: active instruments, then every investment transaction ordered
    ``(instrument_id, date, id)`` so the per-instrument replay is FIFO with the
    id tie-break. Instruments with no NAV yield ``None`` for current value / P&L.

    ``usd_inr_rate`` is the as-of USD→INR rate used to convert a USD holding's current value
    to INR (``None`` ⇒ no rate cached ⇒ that holding's ``current_value_inr_paise`` is ``None``).
    INR holdings ignore it (rate is exactly 1). Defaulting to ``None`` keeps INR-only callers /
    tests unchanged — an all-INR portfolio is identical with or without it.

    ``as_of`` anchors ``Holding.nav_staleness_days`` and nothing else, and defaults to
    ``None`` for the same reason ``usd_inr_rate`` does: every existing caller and test is
    byte-identical without it, and a caller that has no meaningful "today" (a backfill, a
    unit test) should get ``None`` rather than an age measured against a date it did not
    choose. The route passes ``clock.today()``.
    """
    instruments = list(
        session.scalars(
            select(Instrument).where(
                Instrument.user_id == user_id,
                Instrument.archived_at.is_(None),
            )
        )
    )
    by_id = {inst.id: inst for inst in instruments}

    txns = session.scalars(
        select(InvestmentTransaction)
        .where(InvestmentTransaction.user_id == user_id)
        .order_by(
            InvestmentTransaction.instrument_id,
            InvestmentTransaction.date,
            InvestmentTransaction.id,
        )
    )

    lots_by_instrument: dict[int, deque[_Lot]] = {iid: deque() for iid in by_id}
    for txn in txns:
        lots = lots_by_instrument.get(txn.instrument_id)
        if lots is None:
            continue  # txn against an archived/foreign instrument — skip
        _apply(lots, txn)

    holdings: list[Holding] = []
    for inst in instruments:
        lots = lots_by_instrument[inst.id]
        net_units = sum((lot.units for lot in lots), Decimal("0"))
        if net_units <= 0:
            continue  # fully exited (or never held) — not a current position
        invested_paise = sum(lot.cost_paise for lot in lots)
        # Per-lot historical conversion: each surviving lot's native cost at its own
        # acquisition-date rate, summed. NOT invested_native × today's rate (that would
        # inject FX P&L into the cost basis). INR lots carry rate 1 → exact no-op.
        invested_inr_paise = sum(to_inr_paise(lot.cost_paise, lot.fx_rate_to_inr) for lot in lots)
        # avg cost per unit, native: paise → native rupees, then ÷ units.
        avg_cost = (Decimal(invested_paise) / Decimal(100) / net_units).quantize(
            Decimal("0.00000001"), rounding=ROUND_HALF_EVEN
        )

        # Current-value rate is currency-gated: INR is an exact no-op (rate 1); USD uses
        # the as-of rate. A USD holding with no cached rate → INR value stays None (the
        # FX-unavailable signal), never a silent 1:1 cent↔paise conversion.
        fx_rate = Decimal(1) if inst.currency == "INR" else usd_inr_rate

        current_value: int | None = None
        unrealized: int | None = None
        current_value_inr: int | None = None
        unrealized_inr: int | None = None
        if inst.current_nav is not None:
            current_value = int(
                (net_units * inst.current_nav * 100).to_integral_value(rounding=ROUND_HALF_EVEN)
            )
            unrealized = current_value - invested_paise
            if fx_rate is not None:
                current_value_inr = to_inr_paise(current_value, fx_rate)
                unrealized_inr = current_value_inr - invested_inr_paise

        holdings.append(
            Holding(
                instrument_id=inst.id,
                symbol=inst.symbol,
                name=inst.name,
                asset_class=inst.asset_class,
                currency=inst.currency,
                net_units=net_units,
                avg_cost_native=avg_cost,
                invested_native_paise=invested_paise,
                current_nav=inst.current_nav,
                current_value_native_paise=current_value,
                unrealized_pnl_native_paise=unrealized,
                invested_inr_paise=invested_inr_paise,
                current_value_inr_paise=current_value_inr,
                unrealized_pnl_inr_paise=unrealized_inr,
                # A one-element fold, so the per-row age and both portfolio-wide numbers
                # can never disagree about what "how old" means.
                nav_staleness_days=(
                    max_staleness_days([inst.nav_updated_at], as_of=as_of)
                    if as_of is not None and inst.current_nav is not None
                    else None
                ),
            )
        )

    holdings.sort(key=lambda h: h.symbol)
    return holdings


def summarize_holdings(holdings: list[Holding]) -> HoldingsValueRollup:
    """Roll up holdings into portfolio value tiles in **INR** (NAV-bearing set only).

    Sums the per-holding ``*_inr_paise`` fields (not the native ones), so a mixed-currency
    portfolio rolls up correctly. ``unrealized_pnl_paise`` is ``current_value - invested``
    over the NAV-bearing set — equal to the sum of per-holding INR P&L (integer subtraction,
    no rounding). A holding excluded from the totals is either genuinely unpriced
    (``null_nav_count``) or priced-but-FX-unavailable (``fx_unavailable_count``); the split
    lets the response say which.
    """
    current_value = 0
    invested = 0
    nav_bearing = 0
    null_nav = 0
    fx_unavailable = 0
    for h in holdings:
        if h.current_value_inr_paise is None:
            # current_nav set but no INR value ⇒ a USD holding with no cached FX rate;
            # current_nav None ⇒ genuinely unpriced.
            if h.current_nav is not None:
                fx_unavailable += 1
            else:
                null_nav += 1
            continue
        nav_bearing += 1
        current_value += h.current_value_inr_paise
        invested += h.invested_inr_paise
    return HoldingsValueRollup(
        current_value_paise=current_value,
        invested_paise=invested,
        unrealized_pnl_paise=current_value - invested,
        holdings_count=nav_bearing,
        null_nav_count=null_nav,
        fx_unavailable_count=fx_unavailable,
    )


# The single authority for how each transaction type moves units, replacing four
# hand-mirrored spellings across three modules (A2.7/A4.1). buy/sip/switch_in/bonus
# add units; sell/switch_out remove them; dividend (cash) and split (deferred
# rescale) are unit-neutral. Ordered as ``InvestmentTxnTypeStr`` declares its members
# so the correspondence is eyeball-checkable.
#
# A 9th member is caught at CI in two places, which is what makes this one authority
# rather than a fourth copy: ``test_unit_sign_covers_every_member`` fails if it is
# missing from this map, and ``test_apply_unit_effect_matches_unit_sign`` fails if it
# is in the map but has no :func:`_apply` branch.
#
# Read it with ``.get(t, 0)``, never ``[t]``. This partition folds *stored* rows on
# GET /holdings, /portfolio and /dashboards/overview, and the read-model never
# crashes on bad data (see :func:`_consume_fifo`) — an unknown value is already
# unreachable via the ``investment_txn_type`` CHECK, so a KeyError would only trade a
# logged degrade for a 500. Cash *direction* is a different partition and lives in
# :func:`app.services.portfolio_service._signed_cashflow` — dividend is +cash with
# zero units, bonus is zero cash with units. Don't merge them.
UNIT_SIGN: dict[InvestmentTxnTypeStr, int] = {
    "buy": 1,
    "sell": -1,
    "sip": 1,
    "dividend": 0,
    "bonus": 1,
    "split": 0,
    "switch_in": 1,
    "switch_out": -1,
}


def available_units(session: Session, *, user_id: UUID, instrument_id: int) -> Decimal:
    """Net units currently held for one instrument — the write-time oversell guard's
    source of truth (a ``sell`` / ``switch_out`` may not exceed this).

    Sums units the same way :func:`_apply` folds them — both read the sign from
    ``UNIT_SIGN`` — reading each row's ``units`` as an exact ``Decimal``. The
    ``Units`` column round-trips through a scaled int, so this sums in Python over
    the selected column (where the decorator unscales) rather than via a SQL
    aggregate (which would sum the raw scaled ints without unscaling). Returns
    ``Decimal("0")`` for an instrument with no transactions.

    Scope note: this is the *current total* net, not the net as-of a given date, so
    a back-dated sell is validated against today's balance — the accepted v1
    simplification (a true as-of-date FIFO check would duplicate ``_consume_fifo``).
    """
    rows = session.execute(
        select(InvestmentTransaction.transaction_type, InvestmentTransaction.units).where(
            InvestmentTransaction.user_id == user_id,
            InvestmentTransaction.instrument_id == instrument_id,
        )
    ).all()
    net = Decimal("0")
    for txn_type, units in rows:
        net += UNIT_SIGN.get(txn_type, 0) * units
    return net


def _apply(lots: deque[_Lot], txn: InvestmentTransaction) -> None:
    """Fold one transaction into an instrument's FIFO lot queue (in place).

    The *sign* of each branch below comes from ``UNIT_SIGN``; the branches exist
    because they differ in **cost basis**, which a sign cannot express — ``bonus``
    opens a zero-cost lot while ``buy`` / ``sip`` / ``switch_in`` carry acquisition
    cost. So this cannot be collapsed into a single signed expression, and
    ``test_apply_unit_effect_matches_unit_sign`` is what keeps the two bound.
    """
    t = txn.transaction_type
    if t in ("buy", "sip", "switch_in"):
        # switch_in is the acquire-side of a CAS scheme switch: units enter at the
        # switched-in cost, exactly like a buy/sip (PRD §F7 248-251).
        lots.append(
            _Lot(
                units=txn.units,
                cost_paise=txn.amount_native_paise + txn.fees_native_paise,
                fx_rate_to_inr=txn.fx_rate_to_inr,
            )
        )
    elif t == "bonus":
        # Free units, zero cost — fx is moot (to_inr_paise(0, rate) == 0) but carried for shape.
        lots.append(_Lot(units=txn.units, cost_paise=0, fx_rate_to_inr=txn.fx_rate_to_inr))
    elif t in ("sell", "switch_out"):
        # switch_out is the release-side of a switch: units leave the source scheme
        # FIFO, exactly like a sell (PRD §F7 248-251).
        _consume_fifo(lots, txn.units, txn)
    # dividend: cash payout, no lot effect.
    # split: deferred (PRD §F7 244-247 = rescale prior lots). No importer produces a
    # `split` row today (the CSV importer and manual entry both reject it), so this
    # branch is unreachable — kept for when a corporate-action source lands.
    # TODO(F7): implement rescale if a need lands.
    elif t == "split":
        logger.warning("holdings.unsupported_type_ignored", txn_type=t, txn_id=txn.id)


def _consume_fifo(lots: deque[_Lot], qty: Decimal, txn: InvestmentTransaction) -> None:
    """Remove ``qty`` units from the front of the lot queue, releasing cost basis.

    Whole lots are popped; the boundary lot releases a proportional share of its
    remaining cost. An oversell (more units than held) clamps at empty and logs —
    the read-model never crashes on bad data; write-time validation is deferred.
    """
    remaining = qty
    while remaining > 0 and lots:
        lot = lots[0]
        if lot.units <= remaining:
            remaining -= lot.units
            lots.popleft()
        else:
            released = int(
                (Decimal(lot.cost_paise) * remaining / lot.units).to_integral_value(
                    rounding=ROUND_HALF_EVEN
                )
            )
            lot.cost_paise -= released
            lot.units -= remaining
            remaining = Decimal("0")
    if remaining > 0:
        logger.warning(
            "holdings.oversell_clamped",
            instrument_id=txn.instrument_id,
            txn_id=txn.id,
            unmatched_units=str(remaining),
        )
