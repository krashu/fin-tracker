"""Service-level tests for the FIFO holdings read-model (PRD §F7).

Exercises ``app.services.holdings_service.compute_holdings`` directly against an
in-memory session — no TestClient, no router (services tests don't pick up the
api/conftest.py rig). This is the coverage-gated module for the slice, so every
branch of the FIFO replay is hit: buy/sip lots, partial-sell proportional cost
release, the same-date id-ascending tie-break, bonus (free units), dividend
(no-op), NAV-None, oversell clamp, the deferred split/switch no-op, and the per-row
valuation age.
"""

from __future__ import annotations

import uuid
from collections import deque
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import get_args
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from app.models import Instrument, InvestmentTransaction, User
from app.models.instrument import AssetClassStr
from app.models.investment_transaction import InvestmentTxnTypeStr
from app.services.holdings_service import (
    UNIT_SIGN,
    _apply,
    available_units,
    compute_holdings,
    max_staleness_days,
    summarize_holdings,
)
from app.services.nav_snapshot_service import as_valuation_stamp

_D = date(2026, 6, 1)


def _instrument(
    session: Session,
    user_id: UUID,
    *,
    symbol: str = "INF001",
    asset_class: AssetClassStr = "indian_mf",
    current_nav: Decimal | None = None,
    currency: str = "INR",
    exchange: str = "MFCentral",
    nav_updated_at: datetime | None = None,
) -> Instrument:
    inst = Instrument(
        user_id=user_id,
        symbol=symbol,
        name=f"{symbol} Fund",
        asset_class=asset_class,
        currency=currency,
        exchange=exchange,
        current_nav=current_nav,
        nav_updated_at=nav_updated_at,
    )
    session.add(inst)
    session.flush()
    return inst


def _txn(
    session: Session,
    user_id: UUID,
    instrument_id: int,
    *,
    txn_type: InvestmentTxnTypeStr,
    units: Decimal = Decimal("0"),
    amount_paise: int = 0,
    fees_paise: int = 0,
    price: Decimal | None = None,
    on: date = _D,
    fx_rate: Decimal = Decimal("1"),
) -> InvestmentTransaction:
    txn = InvestmentTransaction(
        user_id=user_id,
        instrument_id=instrument_id,
        date=on,
        transaction_type=txn_type,
        units=units,
        price_per_unit_native=price,
        amount_native_paise=amount_paise,
        fees_native_paise=fees_paise,
        fx_rate_to_inr=fx_rate,
    )
    session.add(txn)
    session.flush()
    return txn


def _usd_instrument(session: Session, user_id: UUID, **kw: object) -> Instrument:
    return _instrument(
        session,
        user_id,
        symbol="AAPL",
        asset_class="us_equity",
        currency="USD",
        exchange="NASDAQ",
        **kw,  # type: ignore[arg-type]
    )


def test_empty_returns_no_holdings(session: Session, user: User) -> None:
    assert compute_holdings(session, user_id=user.id) == []


def test_single_buy_with_nav(session: Session, user: User) -> None:
    inst = _instrument(session, user.id, current_nav=Decimal("150"))
    _txn(session, user.id, inst.id, txn_type="buy", units=Decimal("10"), amount_paise=100_000)

    (h,) = compute_holdings(session, user_id=user.id)
    assert h.net_units == Decimal("10")
    assert h.invested_native_paise == 100_000
    assert h.avg_cost_native == Decimal("100")  # ₹1000 / 10 units
    assert h.current_value_native_paise == 150_000  # 10 * 150 * 100
    assert h.unrealized_pnl_native_paise == 50_000


def test_full_sell_drops_the_holding(session: Session, user: User) -> None:
    inst = _instrument(session, user.id, current_nav=Decimal("150"))
    _txn(session, user.id, inst.id, txn_type="buy", units=Decimal("10"), amount_paise=100_000)
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="sell",
        units=Decimal("10"),
        amount_paise=160_000,
        price=Decimal("160"),
        on=date(2026, 6, 2),
    )

    assert compute_holdings(session, user_id=user.id) == []


def test_partial_sell_releases_fifo_cost_basis(session: Session, user: User) -> None:
    inst = _instrument(session, user.id, current_nav=Decimal("150"))
    # Two lots: 10 @ ₹1000 then 10 @ ₹2000.
    _txn(session, user.id, inst.id, txn_type="buy", units=Decimal("10"), amount_paise=100_000)
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=200_000,
        on=date(2026, 6, 2),
    )
    # Sell 15: lot1 (10) fully + 5 of lot2 → release 100000 + (200000 * 5/10)=100000.
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="sell",
        units=Decimal("15"),
        amount_paise=300_000,
        price=Decimal("200"),
        on=date(2026, 6, 3),
    )

    (h,) = compute_holdings(session, user_id=user.id)
    assert h.net_units == Decimal("5")
    assert h.invested_native_paise == 100_000  # half of lot2 remains
    assert h.current_value_native_paise == 75_000  # 5 * 150 * 100
    assert h.unrealized_pnl_native_paise == -25_000


def test_same_date_tiebreak_consumes_lower_id_first(session: Session, user: User) -> None:
    inst = _instrument(session, user.id)
    # Same date; buy_a created first (lower id) is cheaper, buy_b dearer.
    _txn(session, user.id, inst.id, txn_type="buy", units=Decimal("10"), amount_paise=100_000)
    _txn(session, user.id, inst.id, txn_type="buy", units=Decimal("10"), amount_paise=300_000)
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="sell",
        units=Decimal("10"),
        amount_paise=150_000,
        price=Decimal("150"),
    )

    (h,) = compute_holdings(session, user_id=user.id)
    assert h.net_units == Decimal("10")
    # Lower-id (cheap) lot consumed first → the dear lot remains. Wrong tie-break
    # would leave invested == 100_000.
    assert h.invested_native_paise == 300_000


def test_bonus_adds_units_at_zero_cost(session: Session, user: User) -> None:
    inst = _instrument(session, user.id)
    _txn(session, user.id, inst.id, txn_type="buy", units=Decimal("10"), amount_paise=100_000)
    _txn(session, user.id, inst.id, txn_type="bonus", units=Decimal("5"), on=date(2026, 6, 2))

    (h,) = compute_holdings(session, user_id=user.id)
    assert h.net_units == Decimal("15")
    assert h.invested_native_paise == 100_000  # cost unchanged; avg cost drops
    assert h.avg_cost_native == Decimal("66.66666667")  # ₹1000 / 15, 8dp half-even


def test_dividend_does_not_change_the_holding(session: Session, user: User) -> None:
    inst = _instrument(session, user.id)
    _txn(session, user.id, inst.id, txn_type="buy", units=Decimal("10"), amount_paise=100_000)
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="dividend",
        units=Decimal("0"),
        amount_paise=5_000,
        on=date(2026, 6, 2),
    )

    (h,) = compute_holdings(session, user_id=user.id)
    assert h.net_units == Decimal("10")
    assert h.invested_native_paise == 100_000


def test_nav_none_yields_null_value_and_pnl(session: Session, user: User) -> None:
    inst = _instrument(session, user.id, current_nav=None)
    _txn(session, user.id, inst.id, txn_type="buy", units=Decimal("10"), amount_paise=100_000)

    (h,) = compute_holdings(session, user_id=user.id)
    assert h.net_units == Decimal("10")
    assert h.invested_native_paise == 100_000
    assert h.current_nav is None
    assert h.current_value_native_paise is None
    assert h.unrealized_pnl_native_paise is None


def test_oversell_clamps_without_crashing(session: Session, user: User) -> None:
    inst = _instrument(session, user.id, current_nav=Decimal("150"))
    _txn(session, user.id, inst.id, txn_type="buy", units=Decimal("10"), amount_paise=100_000)
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="sell",
        units=Decimal("15"),
        amount_paise=225_000,
        price=Decimal("150"),
        on=date(2026, 6, 2),
    )

    # All units consumed, the excess 5 is clamped (logged) — no negative position.
    assert compute_holdings(session, user_id=user.id) == []


def test_fees_are_included_in_cost_basis(session: Session, user: User) -> None:
    inst = _instrument(session, user.id)
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        fees_paise=500,
    )

    (h,) = compute_holdings(session, user_id=user.id)
    assert h.invested_native_paise == 100_500


def test_split_is_ignored(session: Session, user: User) -> None:
    inst = _instrument(session, user.id)
    _txn(session, user.id, inst.id, txn_type="buy", units=Decimal("10"), amount_paise=100_000)
    # no importer produces `split` today; it stays a deferred no-op (PRD §F7).
    _txn(session, user.id, inst.id, txn_type="split", units=Decimal("0"), on=date(2026, 6, 2))

    (h,) = compute_holdings(session, user_id=user.id)
    assert h.net_units == Decimal("10")
    assert h.invested_native_paise == 100_000


# D3 golden scenario, hand-computed once and reused across the holdings and XIRR
# tests: buy 10 units @ ₹100 = ₹1000 on _REINVEST_FUNDED, then an IDCW of ₹100
# reinvested at NAV ₹125 → 0.8 units on _REINVEST_ON.
_REINVEST_FUNDED = date(2025, 6, 19)
_REINVEST_ON = date(2025, 12, 19)


def test_bare_dividend_understates_units_and_invested(session: Session, user: User) -> None:
    """D3's downstream damage, pinned as the "before" comparison.

    A cash-only ``dividend`` is the sole shape available for an IDCW-reinvest plan
    before the pair exists. It drifts from the AMC statement in BOTH directions and
    raises no error: the statement says 10.8 units and ₹1100 invested.
    """
    inst = _instrument(session, user.id, current_nav=Decimal("150"))
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        price=Decimal("100"),
        on=_REINVEST_FUNDED,
    )
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="dividend",
        amount_paise=10_000,
        on=_REINVEST_ON,
    )

    (h,) = compute_holdings(session, user_id=user.id)
    assert h.net_units == Decimal("10")  # AMC statement: 10.8
    assert h.invested_native_paise == 100_000  # AMC statement: 110_000


def test_reinvestment_pair_adds_a_lot_at_its_own_cost(session: Session, user: User) -> None:
    """The dividend leg is unit- and cost-neutral; the buy leg carries the basis."""
    inst = _instrument(session, user.id, current_nav=Decimal("150"))
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        price=Decimal("100"),
        on=_REINVEST_FUNDED,
    )
    _txn(session, user.id, inst.id, txn_type="dividend", amount_paise=10_000, on=_REINVEST_ON)
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("0.8"),
        amount_paise=10_000,
        price=Decimal("125"),
        on=_REINVEST_ON,
    )

    (h,) = compute_holdings(session, user_id=user.id)
    assert h.net_units == Decimal("10.8")
    assert h.invested_native_paise == 110_000
    assert h.avg_cost_native == Decimal("101.85185185")  # 1100 / 10.8, 8 dp


def test_reinvest_lot_is_separate_from_the_funding_lot(session: Session, user: User) -> None:
    """The assertion that proves "own cost basis" — and the one a naive fix fails.

    Selling exactly the funding lot's 10 units must leave the reinvest lot whole:
    0.8 units at its own ₹100 cost, because FIFO consumed the first lot entirely.
    Had the units been folded onto the dividend row instead of opening a real lot,
    the remaining cost would be the blended 110_000 × 0.8/10.8 = 8_148 paise. The
    two answers differ, so this can only pass with a genuinely separate lot.
    """
    inst = _instrument(session, user.id, current_nav=Decimal("150"))
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        price=Decimal("100"),
        on=_REINVEST_FUNDED,
    )
    _txn(session, user.id, inst.id, txn_type="dividend", amount_paise=10_000, on=_REINVEST_ON)
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("0.8"),
        amount_paise=10_000,
        price=Decimal("125"),
        on=_REINVEST_ON,
    )
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="sell",
        units=Decimal("10"),
        amount_paise=150_000,
        price=Decimal("150"),
        on=date(2026, 1, 15),
    )

    (h,) = compute_holdings(session, user_id=user.id)
    assert h.net_units == Decimal("0.8")
    assert h.invested_native_paise == 10_000  # NOT 8_148 (the blended-lot answer)


def test_reinvest_lot_acquisition_date_orders_between_neighbours(
    session: Session, user: User
) -> None:
    """The reinvest lot sits at its own date in FIFO order, not at the funding date.

    Buy 10 (D1) → reinvest 0.8 (D2) → buy 5 (D3), then sell 10.8: FIFO must have
    consumed exactly the first two lots, leaving the D3 lot untouched.
    """
    inst = _instrument(session, user.id, current_nav=Decimal("150"))
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        price=Decimal("100"),
        on=_REINVEST_FUNDED,
    )
    _txn(session, user.id, inst.id, txn_type="dividend", amount_paise=10_000, on=_REINVEST_ON)
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("0.8"),
        amount_paise=10_000,
        price=Decimal("125"),
        on=_REINVEST_ON,
    )
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("5"),
        amount_paise=70_000,
        price=Decimal("140"),
        on=date(2026, 3, 1),
    )
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="sell",
        units=Decimal("10.8"),
        amount_paise=160_000,
        price=Decimal("148"),
        on=date(2026, 4, 1),
    )

    (h,) = compute_holdings(session, user_id=user.id)
    assert h.net_units == Decimal("5")
    assert h.invested_native_paise == 70_000  # exactly the D3 lot


def test_switch_in_adds_and_switch_out_consumes_fifo(session: Session, user: User) -> None:
    inst = _instrument(session, user.id)
    _txn(session, user.id, inst.id, txn_type="buy", units=Decimal("10"), amount_paise=100_000)
    # switch_in acquires units like a buy (PRD §F7 248-251).
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="switch_in",
        units=Decimal("5"),
        amount_paise=80_000,
        price=Decimal("16"),
        on=date(2026, 6, 3),
    )
    # switch_out releases units FIFO like a sell.
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="switch_out",
        units=Decimal("3"),
        amount_paise=50_000,
        price=Decimal("16"),
        on=date(2026, 6, 4),
    )

    (h,) = compute_holdings(session, user_id=user.id)
    assert h.net_units == Decimal("12")  # 10 + 5 − 3
    # Remaining FIFO cost: first lot 100_000×7/10 = 70_000, plus switch_in lot 80_000.
    assert h.invested_native_paise == 150_000


def test_multiple_instruments_independent_and_symbol_sorted(session: Session, user: User) -> None:
    z = _instrument(session, user.id, symbol="ZZZ")
    a = _instrument(session, user.id, symbol="AAA")
    _txn(session, user.id, z.id, txn_type="buy", units=Decimal("1"), amount_paise=10_000)
    _txn(session, user.id, a.id, txn_type="buy", units=Decimal("2"), amount_paise=20_000)

    holdings = compute_holdings(session, user_id=user.id)
    assert [h.symbol for h in holdings] == ["AAA", "ZZZ"]
    assert holdings[0].net_units == Decimal("2")
    assert holdings[1].net_units == Decimal("1")


def test_archived_instrument_excluded(session: Session, user: User) -> None:
    from datetime import UTC, datetime

    inst = _instrument(session, user.id)
    _txn(session, user.id, inst.id, txn_type="buy", units=Decimal("10"), amount_paise=100_000)
    inst.archived_at = datetime.now(UTC)
    session.flush()

    assert compute_holdings(session, user_id=user.id) == []


# --- summarize_holdings (portfolio value rollup) -----------------------------


def test_summarize_empty_is_all_zero(session: Session, user: User) -> None:
    rollup = summarize_holdings(compute_holdings(session, user_id=user.id))
    assert rollup.current_value_paise == 0
    assert rollup.invested_paise == 0
    assert rollup.unrealized_pnl_paise == 0
    assert rollup.holdings_count == 0
    assert rollup.null_nav_count == 0


def test_summarize_sums_nav_bearing_and_counts_null_nav(session: Session, user: User) -> None:
    # NAV-bearing: buy 10 @ ₹1000, NAV 150 → value 150_000, invested 100_000, pnl +50_000.
    priced = _instrument(session, user.id, symbol="AAA", current_nav=Decimal("150"))
    _txn(session, user.id, priced.id, txn_type="buy", units=Decimal("10"), amount_paise=100_000)
    # Null-NAV: held but unpriced → excluded from the sums, counted separately.
    unpriced = _instrument(session, user.id, symbol="BBB", current_nav=None)
    _txn(session, user.id, unpriced.id, txn_type="buy", units=Decimal("5"), amount_paise=60_000)

    rollup = summarize_holdings(compute_holdings(session, user_id=user.id))
    assert rollup.current_value_paise == 150_000  # only the priced holding
    assert rollup.invested_paise == 100_000  # null-NAV's 60_000 excluded
    assert rollup.unrealized_pnl_paise == 50_000
    assert rollup.holdings_count == 1  # NAV-bearing only
    assert rollup.null_nav_count == 1


def test_summarize_all_null_nav_is_zero_value_with_count(session: Session, user: User) -> None:
    inst = _instrument(session, user.id, current_nav=None)
    _txn(session, user.id, inst.id, txn_type="buy", units=Decimal("10"), amount_paise=100_000)

    rollup = summarize_holdings(compute_holdings(session, user_id=user.id))
    assert rollup.current_value_paise == 0
    assert rollup.invested_paise == 0
    assert rollup.unrealized_pnl_paise == 0
    assert rollup.holdings_count == 0
    assert rollup.null_nav_count == 1


# --------------------------------------------------------------------------- #
# FX / multi-currency (PRD §F7 v0.5)
# --------------------------------------------------------------------------- #
def test_inr_holding_inr_fields_equal_native(session: Session, user: User) -> None:
    # The backward-compat no-op: for an INR holding every *_inr_paise == its *_native_paise,
    # regardless of any usd_inr_rate passed (currency-gated to rate 1).
    inst = _instrument(session, user.id, current_nav=Decimal("150"))
    _txn(session, user.id, inst.id, txn_type="buy", units=Decimal("10"), amount_paise=100_000)

    [h] = compute_holdings(session, user_id=user.id, usd_inr_rate=Decimal("83"))
    assert h.invested_inr_paise == h.invested_native_paise == 100_000
    assert h.current_value_inr_paise == h.current_value_native_paise == 150_000
    assert h.unrealized_pnl_inr_paise == h.unrealized_pnl_native_paise == 50_000


def test_usd_holding_converts_at_historical_and_as_of_rates(session: Session, user: User) -> None:
    # Buy at fx 80 (historical cost basis); value at fx 85 (as-of). Cost basis must use 80,
    # current value must use 85 — never one rate for both.
    inst = _usd_instrument(session, user.id, current_nav=Decimal("200"))  # $200/share
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        fx_rate=Decimal("80"),  # $1000 @ ₹80
    )

    [h] = compute_holdings(session, user_id=user.id, usd_inr_rate=Decimal("85"))
    assert h.invested_native_paise == 100_000  # $1000 in cents
    assert h.current_value_native_paise == 200_000  # 10 * $200 in cents
    assert h.invested_inr_paise == 100_000 * 80  # historical rate
    assert h.current_value_inr_paise == 200_000 * 85  # as-of rate
    assert h.unrealized_pnl_inr_paise == 200_000 * 85 - 100_000 * 80


def test_mixed_rate_fifo_invested_is_per_lot_historical(session: Session, user: User) -> None:
    # Two USD buys at different rates, then a partial sell. The surviving lots keep their own
    # acquisition rates: invested_inr = Σ(per-lot native cost × that lot's rate), NOT
    # invested_native × today's rate. This is the headline correctness pin.
    inst = _usd_instrument(session, user.id, current_nav=Decimal("200"))
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        fx_rate=Decimal("80"),
    )  # lot1: $1000 @ 80
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=120_000,
        fx_rate=Decimal("84"),
    )  # lot2: $1200 @ 84
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="sell",
        units=Decimal("5"),
        amount_paise=110_000,
        price=Decimal("220"),
    )  # FIFO consumes 5 from lot1

    [h] = compute_holdings(session, user_id=user.id, usd_inr_rate=Decimal("85"))
    # Surviving: lot1 5u/$500 @ 80, lot2 10u/$1200 @ 84.
    assert h.net_units == Decimal("15")
    assert h.invested_native_paise == 50_000 + 120_000
    assert h.invested_inr_paise == 50_000 * 80 + 120_000 * 84  # per-lot historical
    # A single-rate shortcut (today's 85 × native) would give a different, wrong number.
    assert h.invested_inr_paise != (50_000 + 120_000) * 85


def test_usd_holding_no_rate_is_fx_unavailable(session: Session, user: User) -> None:
    # A priced USD holding with no cached FX rate (usd_inr_rate=None) yields no INR value and
    # is tallied in fx_unavailable_count — distinct from a genuinely-unpriced (null_nav) holding.
    inst = _usd_instrument(session, user.id, current_nav=Decimal("200"))
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        fx_rate=Decimal("80"),
    )

    [h] = compute_holdings(session, user_id=user.id, usd_inr_rate=None)
    assert h.current_value_native_paise == 200_000  # priced natively
    assert h.current_value_inr_paise is None  # but not convertible to INR
    assert h.unrealized_pnl_inr_paise is None

    rollup = summarize_holdings([h])
    assert rollup.fx_unavailable_count == 1
    assert rollup.null_nav_count == 0
    assert rollup.holdings_count == 0  # excluded from the NAV-bearing rollup
    assert rollup.current_value_paise == 0


def test_summarize_mixed_currency_rolls_up_to_inr(session: Session, user: User) -> None:
    # An INR holding + a USD holding roll up to a single INR total (USD converted at as-of rate).
    inr = _instrument(session, user.id, symbol="INF1", current_nav=Decimal("150"))
    _txn(session, user.id, inr.id, txn_type="buy", units=Decimal("10"), amount_paise=100_000)
    usd = _usd_instrument(session, user.id, current_nav=Decimal("200"))
    _txn(
        session,
        user.id,
        usd.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        fx_rate=Decimal("80"),
    )

    rollup = summarize_holdings(
        compute_holdings(session, user_id=user.id, usd_inr_rate=Decimal("85"))
    )
    assert rollup.holdings_count == 2
    assert rollup.fx_unavailable_count == 0
    # INR 150_000 + USD (200_000 cents × 85) INR paise.
    assert rollup.current_value_paise == 150_000 + 200_000 * 85
    assert rollup.invested_paise == 100_000 + 100_000 * 80


# ---------- available_units (the write-time oversell guard) -------------------
#
# Zero tests referenced this function before, and coverage reported the whole
# unit-subtracting branch as never executed — while it has TWO production callers:
# the manual-sell 422 in api/v1/investment_transactions.py and the CSV importer's
# per-instrument oversell prefetch in investment_import_service.py.
#
# It is also the ONLY place where `bonus` counts toward sellable units. The four
# hand-mirrored spellings of that rule are now one `UNIT_SIGN` map (A2.7/A4.1), so the
# divergence these tests were bought to catch can no longer be introduced by editing
# one site — but they still pin the *value* of the rule (bonus is sellable), which no
# consolidation can prove on its own. The two exhaustiveness tests below are what keep
# `UNIT_SIGN` and `_apply` bound to each other.


def test_available_units_counts_bonus_as_sellable(session: Session, user: User) -> None:
    """buy(10) + bonus(5) → 15 sellable. Bonus units ARE sellable."""
    inst = _instrument(session, user.id)
    _txn(session, user.id, inst.id, txn_type="buy", units=Decimal("10"), amount_paise=100_000)
    _txn(session, user.id, inst.id, txn_type="bonus", units=Decimal("5"))

    assert available_units(session, user_id=user.id, instrument_id=inst.id) == Decimal("15")


def test_available_units_subtracts_sell_and_switch_out(session: Session, user: User) -> None:
    """The subtract branch: buy(10) − sell(4) → 6, and switch_out subtracts too."""
    inst = _instrument(session, user.id)
    _txn(session, user.id, inst.id, txn_type="buy", units=Decimal("10"), amount_paise=100_000)
    _txn(session, user.id, inst.id, txn_type="sell", units=Decimal("4"), amount_paise=50_000)

    assert available_units(session, user_id=user.id, instrument_id=inst.id) == Decimal("6")

    _txn(session, user.id, inst.id, txn_type="switch_out", units=Decimal("2"), amount_paise=25_000)
    assert available_units(session, user_id=user.id, instrument_id=inst.id) == Decimal("4")


def test_available_units_ignores_unit_neutral_types(session: Session, user: User) -> None:
    """dividend is cash and split is deferred — neither moves the sellable balance."""
    inst = _instrument(session, user.id)
    _txn(session, user.id, inst.id, txn_type="buy", units=Decimal("10"), amount_paise=100_000)
    _txn(session, user.id, inst.id, txn_type="dividend", amount_paise=5_000)
    _txn(session, user.id, inst.id, txn_type="split", units=Decimal("90"))

    assert available_units(session, user_id=user.id, instrument_id=inst.id) == Decimal("10")


def test_available_units_is_user_and_instrument_scoped(session: Session, user: User) -> None:
    """Another user's rows, and another instrument's rows, never leak in."""
    other = User(id=uuid.uuid4())
    session.add(other)
    session.flush()

    mine = _instrument(session, user.id, symbol="INF_MINE")
    theirs = _instrument(session, other.id, symbol="INF_THEIRS")
    sibling = _instrument(session, user.id, symbol="INF_SIBLING")

    _txn(session, user.id, mine.id, txn_type="buy", units=Decimal("10"), amount_paise=100_000)
    _txn(session, other.id, theirs.id, txn_type="buy", units=Decimal("99"), amount_paise=100_000)
    _txn(session, user.id, sibling.id, txn_type="buy", units=Decimal("77"), amount_paise=100_000)

    assert available_units(session, user_id=user.id, instrument_id=mine.id) == Decimal("10")


def test_available_units_zero_for_instrument_with_no_transactions(
    session: Session, user: User
) -> None:
    inst = _instrument(session, user.id)
    assert available_units(session, user_id=user.id, instrument_id=inst.id) == Decimal("0")


# ---------- UNIT_SIGN exhaustiveness (the CI gate that replaced four spellings) ----
#
# `UNIT_SIGN` is deliberately read with `.get(t, 0)` everywhere and `_apply` has no
# `else: raise`, because both fold *stored* rows on read paths where the read-model
# never crashes on bad data (_consume_fifo's docstring). That makes these two tests the
# ONLY gate on a widened `InvestmentTxnTypeStr` — there is no runtime failure to fall
# back on. They catch different mistakes and neither subsumes the other.


def test_unit_sign_covers_every_member() -> None:
    """A 9th member of the Literal must be given a sign, not silently default to 0.

    Without this, widening the enum makes the new type unit-neutral everywhere:
    compute_holdings drops the instrument (net_units <= 0) while available_units counts
    its units anyway, so the oversell guard permits selling a position /holdings says
    does not exist.
    """
    assert set(UNIT_SIGN) == set(get_args(InvestmentTxnTypeStr))


@pytest.mark.parametrize("txn_type", get_args(InvestmentTxnTypeStr))
def test_apply_unit_effect_matches_unit_sign(
    session: Session, user: User, txn_type: InvestmentTxnTypeStr
) -> None:
    """``_apply``'s net unit effect must equal ``UNIT_SIGN[t] * units`` for every member.

    ``_apply`` cannot be driven by the sign alone — ``bonus`` opens a zero-cost lot
    while ``buy``/``sip``/``switch_in`` carry cost — so the two are bound by assertion
    rather than by construction. This is what catches a member added to ``UNIT_SIGN``
    but given no ``_apply`` branch, which ``test_unit_sign_covers_every_member`` cannot
    see.
    """
    inst = _instrument(session, user.id)
    lots: deque = deque()
    _apply(
        lots,
        _txn(
            session, user.id, inst.id, txn_type="buy", units=Decimal("100"), amount_paise=1_000_000
        ),
    )
    assert sum(lot.units for lot in lots) == Decimal("100")

    _apply(
        lots,
        _txn(
            session, user.id, inst.id, txn_type=txn_type, units=Decimal("10"), amount_paise=50_000
        ),
    )

    expected = Decimal("100") + UNIT_SIGN[txn_type] * Decimal("10")
    assert sum(lot.units for lot in lots) == expected


# ---------- per-row valuation age -------------------------------------------


def test_max_staleness_days_over_a_mixed_population(user: User) -> None:
    """The shared fold, unit-tested on the shape both portfolio numbers pass it.

    ``None`` stamps are skipped rather than counted as age 0 — an unpriced holding is not
    a *fresh* one — and an all-``None`` population is ``None``, not 0, so "nothing to
    report" never renders as "everything is current".
    """
    as_of = date(2026, 6, 20)
    stamps = [
        as_valuation_stamp(as_of - timedelta(days=3)),
        None,
        as_valuation_stamp(as_of - timedelta(days=91)),
        as_valuation_stamp(as_of),
    ]
    assert max_staleness_days(stamps, as_of=as_of) == 91
    assert max_staleness_days([], as_of=as_of) is None
    assert max_staleness_days([None, None], as_of=as_of) is None


def test_holding_carries_its_valuation_age(session: Session, user: User) -> None:
    """The /holdings surface: how old is the price behind this row's current value.

    Server-computed, not a raw ``nav_updated_at`` — SQLite hands the column back naive, so
    it serializes with no offset and ``new Date(...)`` in a browser reads it as local time.
    Deriving the age client-side is the same re-derivation that produced the original
    off-by-one.
    """
    as_of = date(2026, 6, 20)
    inst = _instrument(
        session,
        user.id,
        current_nav=Decimal("150"),
        nav_updated_at=as_valuation_stamp(as_of - timedelta(days=91)),
    )
    _txn(session, user.id, inst.id, txn_type="buy", units=Decimal("10"), amount_paise=100_000)

    (h,) = compute_holdings(session, user_id=user.id, as_of=as_of)
    assert h.nav_staleness_days == 91


def test_valuation_age_is_none_without_an_as_of_or_a_nav(session: Session, user: User) -> None:
    """Two independent ``None`` paths, both deliberate.

    No ``as_of`` (the default) keeps every pre-existing caller and test byte-identical —
    the same contract ``usd_inr_rate`` already established — and a caller with no
    meaningful "today" should not get an age measured against a date it did not pick. No
    NAV means there is no valuation to be stale: the row already reports ``current_value``
    as ``None``, and an age of 0 there would read as "priced, and current".
    """
    as_of = date(2026, 6, 20)
    priced = _instrument(
        session,
        user.id,
        current_nav=Decimal("150"),
        nav_updated_at=as_valuation_stamp(as_of - timedelta(days=5)),
    )
    unpriced = _instrument(session, user.id, symbol="INF002", current_nav=None)
    for iid in (priced.id, unpriced.id):
        _txn(session, user.id, iid, txn_type="buy", units=Decimal("10"), amount_paise=100_000)

    no_anchor = {h.symbol: h.nav_staleness_days for h in compute_holdings(session, user_id=user.id)}
    assert no_anchor == {"INF001": None, "INF002": None}

    anchored = {
        h.symbol: h.nav_staleness_days
        for h in compute_holdings(session, user_id=user.id, as_of=as_of)
    }
    assert anchored == {"INF001": 5, "INF002": None}
