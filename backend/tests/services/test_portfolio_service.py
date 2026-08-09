"""Service tests for the portfolio summary (PRD §F8 view 6 / §F9).

Exercises ``portfolio_service.compute_portfolio_summary`` (tiles + asset-class
allocation + XIRR) and the ``_signed_cashflow`` sign map directly against an
in-memory session. This is the coverage-gated module for the slice, so every
branch is hit: empty / not-priced, the priced rollup + allocation grouping, the
full 8-member sign map (incl. fees and bonus/split skip), the ``_safe_xirr``
solvable + unsolvable+warn paths, the NAV partition, and the fully-exited filter.

Mirrors the ``test_holdings_service`` fixture rig (``_instrument`` / ``_txn``).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session
from structlog.testing import capture_logs

from app.models import FxRateQuote, Instrument, InvestmentTransaction, User
from app.models.instrument import AssetClassStr
from app.models.investment_transaction import InvestmentTxnTypeStr
from app.schemas import AssetClassAllocation, HoldingXirr
from app.services.portfolio_service import _signed_cashflow, compute_portfolio_summary

# A buy one year before the as-of date used by the XIRR arms: a 50% gain over
# exactly 365 days solves to XIRR 0.5 (see test_golden_xirr).
_BUY_ON = date(2025, 6, 19)
_AS_OF = date(2026, 6, 19)


def _instrument(
    session: Session,
    user_id: UUID,
    *,
    symbol: str = "INF001",
    asset_class: AssetClassStr = "indian_mf",
    current_nav: Decimal | None = None,
) -> Instrument:
    inst = Instrument(
        user_id=user_id,
        symbol=symbol,
        name=f"{symbol} Fund",
        asset_class=asset_class,
        currency="INR",
        exchange="MFCentral",
        current_nav=current_nav,
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
    on: date = _BUY_ON,
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
        fx_rate_to_inr=Decimal("1"),
    )
    session.add(txn)
    session.flush()
    return txn


def test_empty_portfolio_is_zero(session: Session, user: User) -> None:
    summary = compute_portfolio_summary(session, user_id=user.id, as_of=_AS_OF)
    assert summary.current_value_paise == 0
    assert summary.invested_paise == 0
    assert summary.unrealized_pnl_paise == 0
    assert summary.holdings_count == 0
    assert summary.null_nav_count == 0
    assert summary.xirr is None
    assert summary.allocations == []
    assert summary.holding_xirr == []


def test_reinvestment_pair_nets_to_zero_in_signed_cashflow(session: Session, user: User) -> None:
    """The invariant the whole D3 pair shape rests on, pinned at its exact site.

    A reinvestment is not an external cashflow — no money entered or left the
    portfolio. XIRR solves ``Σ CF_i/(1+r)^(t_i/365) = 0``, so two flows ``+X`` and
    ``−X`` at the *same* ``t`` contribute ``+X·d − X·d = 0`` for every ``r``: the
    solution set is provably unchanged. Netting to zero is the correct answer here,
    not a fudge that happens to cancel.
    """
    inst = _instrument(session, user.id)
    div = _txn(session, user.id, inst.id, txn_type="dividend", amount_paise=10_000)
    buy = _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("0.8"),
        amount_paise=10_000,
        price=Decimal("125"),
    )

    assert _signed_cashflow(div) == 10_000
    assert _signed_cashflow(buy) == -10_000
    assert _signed_cashflow(div) + _signed_cashflow(buy) == 0


def test_golden_xirr_with_a_reinvestment(session: Session, user: User) -> None:
    """The D3 golden scenario end-to-end through the XIRR path.

    Buy ₹1000 (10 units @ ₹100) on _BUY_ON; ₹100 IDCW reinvested at NAV ₹125 → 0.8
    units halfway through; as-of one year later at NAV ₹150. The December pair
    cancels, leaving −100_000 → +162_000 over exactly 365 days, so XIRR is 0.62 —
    while units and invested BOTH reflect the reinvestment, which is the drift the
    bare-dividend workaround caused.
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
        on=_BUY_ON,
    )
    reinvest_on = date(2025, 12, 19)
    _txn(session, user.id, inst.id, txn_type="dividend", amount_paise=10_000, on=reinvest_on)
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("0.8"),
        amount_paise=10_000,
        price=Decimal("125"),
        on=reinvest_on,
    )

    summary = compute_portfolio_summary(session, user_id=user.id, as_of=_AS_OF)
    assert summary.current_value_paise == 162_000  # 10.8 units × ₹150
    assert summary.invested_paise == 110_000
    assert summary.unrealized_pnl_paise == 52_000
    assert summary.xirr is not None
    assert abs(summary.xirr - 0.62) < 0.001


def test_golden_xirr_one_year_50pct_gain(session: Session, user: User) -> None:
    # Buy ₹1000 (100_000 paise), now worth ₹1500 (10 units × NAV 150) one year on.
    inst = _instrument(session, user.id, current_nav=Decimal("150"))
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        price=Decimal("100"),
        on=_BUY_ON,
    )

    summary = compute_portfolio_summary(session, user_id=user.id, as_of=_AS_OF)
    assert summary.current_value_paise == 150_000
    assert summary.invested_paise == 100_000
    assert summary.unrealized_pnl_paise == 50_000
    assert summary.holdings_count == 1
    assert summary.null_nav_count == 0
    assert summary.allocations == [
        AssetClassAllocation(asset_class="indian_mf", value_paise=150_000)
    ]
    # 50% gain over exactly one year → XIRR ≈ 0.5 (PRD success metric: within 0.1%).
    assert summary.xirr is not None
    assert abs(summary.xirr - 0.5) < 0.001
    # Single holding → its XIRR equals the portfolio-wide number.
    assert len(summary.holding_xirr) == 1
    assert summary.holding_xirr[0].instrument_id == inst.id
    assert summary.holding_xirr[0].xirr is not None
    assert abs(summary.holding_xirr[0].xirr - 0.5) < 0.001


def test_all_null_nav_is_zero_value_and_unsolvable(session: Session, user: User) -> None:
    inst = _instrument(session, user.id, current_nav=None)
    _txn(session, user.id, inst.id, txn_type="buy", units=Decimal("10"), amount_paise=100_000)

    summary = compute_portfolio_summary(session, user_id=user.id, as_of=_AS_OF)
    assert summary.current_value_paise == 0
    assert summary.holdings_count == 0
    assert summary.null_nav_count == 1
    assert summary.xirr is None
    assert summary.allocations == []
    assert summary.holding_xirr == []  # null-NAV holding is not sourced


def test_mixed_nav_partition_and_holding_xirr_cardinality(session: Session, user: User) -> None:
    priced = _instrument(session, user.id, symbol="AAA", current_nav=Decimal("150"))
    _txn(session, user.id, priced.id, txn_type="buy", units=Decimal("10"), amount_paise=100_000)
    unpriced = _instrument(session, user.id, symbol="BBB", current_nav=None)
    _txn(session, user.id, unpriced.id, txn_type="buy", units=Decimal("5"), amount_paise=60_000)

    summary = compute_portfolio_summary(session, user_id=user.id, as_of=_AS_OF)
    assert summary.current_value_paise == 150_000  # only the priced holding
    assert summary.invested_paise == 100_000  # null-NAV's 60_000 excluded
    assert summary.holdings_count == 1
    assert summary.null_nav_count == 1
    # holding_xirr carries exactly the NAV-bearing holding — the null-NAV one is absent.
    assert [hx.instrument_id for hx in summary.holding_xirr] == [priced.id]


def test_dividend_is_a_positive_cashflow(session: Session, user: User) -> None:
    # Price-flat holding (current value == invested): without the dividend XIRR
    # would be 0; the dividend is money returned, so XIRR must be positive.
    inst = _instrument(session, user.id, current_nav=Decimal("100"))
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        price=Decimal("100"),
        on=_BUY_ON,
    )
    _txn(
        session,
        user.id,
        inst.id,
        txn_type="dividend",
        amount_paise=10_000,
        on=date(2025, 12, 19),
    )

    summary = compute_portfolio_summary(session, user_id=user.id, as_of=_AS_OF)
    assert summary.current_value_paise == 100_000  # flat on price
    assert summary.unrealized_pnl_paise == 0
    assert summary.xirr is not None
    assert summary.xirr > 0  # dividend entered as a positive cashflow


def test_signed_cashflow_sign_map(session: Session, user: User) -> None:
    inst = _instrument(session, user.id)

    def cf(txn_type: InvestmentTxnTypeStr, **kw: object) -> int | None:
        return _signed_cashflow(_txn(session, user.id, inst.id, txn_type=txn_type, **kw))  # type: ignore[arg-type]

    # Money out (acquiring units) is negative, incl. fees.
    assert cf("buy", amount_paise=100_000, fees_paise=500) == -100_500
    assert cf("sip", amount_paise=100_000, fees_paise=500) == -100_500
    assert cf("switch_in", amount_paise=80_000) == -80_000
    # Money in (proceeds) is positive, net of fees.
    assert cf("sell", amount_paise=160_000, fees_paise=500) == 159_500
    assert cf("switch_out", amount_paise=50_000) == 50_000
    # Dividend is a positive payout.
    assert cf("dividend", amount_paise=5_000) == 5_000
    # Unit-only events carry no cashflow.
    assert cf("bonus", units=Decimal("5")) is None
    assert cf("split") is None


def test_zero_nav_holding_is_unsolvable_and_warns_without_amounts(
    session: Session, user: User
) -> None:
    # NAV 0 → terminal cashflow +0, which gives no sign change → InvalidPaymentsError
    # → guarded to None. The holding is still NAV-bearing (current value 0, not None).
    inst = _instrument(session, user.id, current_nav=Decimal("0"))
    _txn(session, user.id, inst.id, txn_type="buy", units=Decimal("10"), amount_paise=100_000)

    with capture_logs() as logs:
        summary = compute_portfolio_summary(session, user_id=user.id, as_of=_AS_OF)

    assert summary.current_value_paise == 0
    assert summary.holdings_count == 1  # current value 0 is priced, not null-NAV
    assert summary.xirr is None
    assert summary.holding_xirr == [HoldingXirr(instrument_id=inst.id, xirr=None)]

    events = [e for e in logs if e["event"] == "portfolio.xirr_unsolved"]
    assert events  # warning fired (holding + portfolio scopes)
    # PII discipline: the event carries scope + instrument_id only, never amounts.
    for e in events:
        assert not any("amount" in key or "paise" in key for key in e)


def test_fully_exited_priced_fund_excluded_from_portfolio_xirr(
    session: Session, user: User
) -> None:
    # Held fund: ₹1000 → ₹1500 over a year → XIRR 0.5.
    held = _instrument(session, user.id, symbol="HELD", current_nav=Decimal("150"))
    _txn(
        session,
        user.id,
        held.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=100_000,
        on=_BUY_ON,
    )

    # Fully-exited but still *priced* fund (net_units 0 → dropped by compute_holdings).
    # Its buy/sell rows must NOT leak into the portfolio cashflow union — a user_id-only
    # re-query would add an unbalanced position and corrupt the portfolio XIRR.
    exited = _instrument(session, user.id, symbol="EXIT", current_nav=Decimal("150"))
    _txn(
        session,
        user.id,
        exited.id,
        txn_type="buy",
        units=Decimal("10"),
        amount_paise=50_000,
        on=date(2025, 1, 1),
    )
    _txn(
        session,
        user.id,
        exited.id,
        txn_type="sell",
        units=Decimal("10"),
        amount_paise=60_000,
        price=Decimal("60"),
        on=date(2025, 3, 1),
    )

    summary = compute_portfolio_summary(session, user_id=user.id, as_of=_AS_OF)
    assert summary.holdings_count == 1  # only the held fund
    assert [hx.instrument_id for hx in summary.holding_xirr] == [held.id]
    # Portfolio XIRR is the held fund's alone — the exited fund contributed nothing.
    assert summary.xirr is not None
    assert abs(summary.xirr - 0.5) < 0.001


def test_multi_instrument_allocation_grouped_and_sorted(session: Session, user: User) -> None:
    mf = _instrument(
        session, user.id, symbol="MF", asset_class="indian_mf", current_nav=Decimal("150")
    )
    _txn(session, user.id, mf.id, txn_type="buy", units=Decimal("10"), amount_paise=100_000)
    gold = _instrument(
        session, user.id, symbol="GOLD", asset_class="gold", current_nav=Decimal("200")
    )
    _txn(session, user.id, gold.id, txn_type="buy", units=Decimal("5"), amount_paise=80_000)

    summary = compute_portfolio_summary(session, user_id=user.id, as_of=_AS_OF)
    # One row per class, sorted by asset_class (gold < indian_mf), raw enum value.
    assert summary.allocations == [
        AssetClassAllocation(asset_class="gold", value_paise=100_000),  # 5 × 200 × 100
        AssetClassAllocation(asset_class="indian_mf", value_paise=150_000),  # 10 × 150 × 100
    ]
    assert summary.current_value_paise == 250_000
    assert summary.holdings_count == 2


def _seed_fx(session: Session, on: date, rate: str) -> None:
    session.add(
        FxRateQuote(
            date=on, from_currency="USD", to_currency="INR", rate=Decimal(rate), source="seed"
        )
    )
    session.flush()


def test_usd_portfolio_rolls_up_to_inr_and_xirr_solvable(session: Session, user: User) -> None:
    # A USD holding: $1000 in (fx 80), now worth $1500 (10 × $150) one year on, valued at the
    # as-of rate (80 here). The summary tiles must be INR, and XIRR must solve (≈0.5: a 50% gain,
    # both legs at the same rate → scale-invariant).
    _seed_fx(session, _BUY_ON, "80")
    inst = Instrument(
        user_id=user.id,
        symbol="AAPL",
        name="Apple",
        asset_class="us_equity",
        currency="USD",
        exchange="NASDAQ",
        current_nav=Decimal("150"),
    )
    session.add(inst)
    session.flush()
    session.add(
        InvestmentTransaction(
            user_id=user.id,
            instrument_id=inst.id,
            date=_BUY_ON,
            transaction_type="buy",
            units=Decimal("10"),
            price_per_unit_native=Decimal("100"),
            amount_native_paise=100_000,
            fees_native_paise=0,
            fx_rate_to_inr=Decimal("80"),
        )
    )
    session.flush()

    summary = compute_portfolio_summary(session, user_id=user.id, as_of=_AS_OF)
    assert summary.current_value_paise == 150_000 * 80  # USD value converted at as-of rate
    assert summary.invested_paise == 100_000 * 80  # cost basis at the historical rate
    assert summary.unrealized_pnl_paise == 150_000 * 80 - 100_000 * 80
    assert summary.fx_unavailable_count == 0
    assert summary.allocations == [
        AssetClassAllocation(asset_class="us_equity", value_paise=150_000 * 80)
    ]
    assert summary.xirr is not None
    assert abs(summary.xirr - 0.5) < 0.001


def test_usd_no_fx_rate_excluded_and_flagged(session: Session, user: User) -> None:
    # No fx_rates seeded ⇒ rate_on returns None ⇒ the USD holding can't be priced in INR:
    # excluded from the totals/XIRR and flagged via fx_unavailable_count (not silently dropped).
    inst = Instrument(
        user_id=user.id,
        symbol="AAPL",
        name="Apple",
        asset_class="us_equity",
        currency="USD",
        exchange="NASDAQ",
        current_nav=Decimal("150"),
    )
    session.add(inst)
    session.flush()
    session.add(
        InvestmentTransaction(
            user_id=user.id,
            instrument_id=inst.id,
            date=_BUY_ON,
            transaction_type="buy",
            units=Decimal("10"),
            price_per_unit_native=Decimal("100"),
            amount_native_paise=100_000,
            fees_native_paise=0,
            fx_rate_to_inr=Decimal("80"),
        )
    )
    session.flush()

    summary = compute_portfolio_summary(session, user_id=user.id, as_of=_AS_OF)
    assert summary.current_value_paise == 0
    assert summary.holdings_count == 0
    assert summary.fx_unavailable_count == 1
    assert summary.null_nav_count == 0
    assert summary.xirr is None  # no priced holding sourced
