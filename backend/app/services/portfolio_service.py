"""Portfolio summary service — XIRR + asset-class allocation (PRD §F8 view 6 / §F9).

Builds the ``/portfolio/summary`` payload from the FIFO holdings read-model
(:func:`compute_holdings`) plus a second, independent fold over
``investment_transactions`` for the money-weighted return (XIRR).

Scope caveats (current-portfolio):

- Only **NAV-bearing current holdings** count. Instruments with no NAV, and any
  fully-exited instrument (``net_units == 0``), are absent from
  :func:`compute_holdings` output and therefore from every figure here. XIRR is
  thus "the return on what I still hold and can price", not a lifetime realized
  return — a fully-sold-but-still-priced fund cannot leak its cashflows into the
  portfolio-wide number because it is never in the sourced set.
- **All money is INR.** Cashflows are converted per-row at each transaction's
  ``fx_rate_to_inr`` (historical clock); the current-value terminals come from the
  INR-converted holdings rollup (as-of clock). An INR row carries rate 1, so an
  all-INR portfolio is byte-identical to the pre-FX behaviour.
- A USD holding with no cached FX rate is excluded from both the rollup and the
  cashflows (it can't be priced in INR), and surfaced via ``fx_unavailable_count`` —
  never silently dropped.
- A dividend *reinvestment* is a linked ``dividend`` + ``buy`` pair, not a separate
  enum member (``POST /investment-transactions/reinvestment``; the ``pair_id`` contract
  lives on the model). The two legs are ``+amount`` and ``-amount`` on the same date, so
  they net to exactly zero here — which is the correct answer, not a coincidence: no
  money entered or left the portfolio. XIRR solves
  ``Σ CF_i/(1+r)^(t_i/365) = 0``, and two opposite flows at the same ``t`` contribute
  ``+X·d − X·d = 0`` for every ``r``, so the solution set is provably unchanged. The
  reinvestment is emphatically *not* unit-neutral, which is why the ``buy`` leg must be a
  real row opening its own FIFO lot.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from uuid import UUID

import pyxirr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.log_config import get_logger
from app.models import InvestmentTransaction
from app.models.instrument import AssetClassStr
from app.schemas import AssetClassAllocation, HoldingXirr, PortfolioSummary
from app.services.fx_math import to_inr_paise
from app.services.fx_service import rate_on
from app.services.holdings_service import Holding, compute_holdings, summarize_holdings

logger = get_logger(__name__)


def compute_portfolio_summary(session: Session, *, user_id: UUID, as_of: date) -> PortfolioSummary:
    """Roll up tiles + allocations + XIRR for ``user_id``, valued at ``as_of`` — INR.

    ``as_of`` dates the terminal (current-value) cashflow of every XIRR; the route
    passes ``clock.today()`` (UTC). NAV-bearing = ``current_value_inr_paise is not None``.
    The USD→INR rate is resolved at ``as_of`` (``rate_on``, carry-forward) so a
    backfill/test values a past portfolio at that date's rate, not today's — which is why
    this keeps ``rate_on`` where ``/holdings`` and the overview tile use ``latest_rate``:
    those are "now" views with no as-of date, this one honours whatever date it is given.
    """
    usd_inr = rate_on(session, on=as_of)
    holdings = compute_holdings(session, user_id=user_id, usd_inr_rate=usd_inr)
    rollup = summarize_holdings(holdings)

    # NAV-bearing holdings, INR current value narrowed to int (null-NAV / FX-unavailable /
    # exited cases never reach here — see module docstring).
    priced: list[tuple[Holding, int]] = []
    for h in holdings:
        value = h.current_value_inr_paise
        if value is not None:
            priced.append((h, value))

    # Asset-class allocation: NAV-bearing current value grouped by class, one row
    # per class, deterministically ordered. Keyed by the raw enum value (not a label).
    by_class: dict[AssetClassStr, int] = defaultdict(int)
    for h, value in priced:
        by_class[h.asset_class] += value
    allocations = [
        AssetClassAllocation(asset_class=ac, value_paise=v) for ac, v in sorted(by_class.items())
    ]

    # XIRR — re-fold investment_transactions for the NAV-bearing instruments only.
    holding_xirr: list[HoldingXirr]
    portfolio_xirr: float | None
    if not priced:
        holding_xirr = []
        portfolio_xirr = None
    else:
        sourced_ids = [h.instrument_id for h, _ in priced]
        cashflows = _source_portfolio_cashflows(session, user_id=user_id, sourced_ids=sourced_ids)

        # Per holding: its own flows + a terminal at its current value.
        holding_xirr = [
            HoldingXirr(
                instrument_id=h.instrument_id,
                xirr=_safe_xirr(
                    [*cashflows[h.instrument_id], (as_of, value)],
                    scope="holding",
                    instrument_id=h.instrument_id,
                ),
            )
            for h, value in priced
        ]

        # Portfolio-wide: union of every sourced flow + ONE terminal (Σ current value).
        union = [flow for flows in cashflows.values() for flow in flows]
        union.append((as_of, rollup.current_value_paise))
        portfolio_xirr = _safe_xirr(union, scope="portfolio")

    return PortfolioSummary(
        current_value_paise=rollup.current_value_paise,
        invested_paise=rollup.invested_paise,
        unrealized_pnl_paise=rollup.unrealized_pnl_paise,
        xirr=portfolio_xirr,
        holdings_count=rollup.holdings_count,
        null_nav_count=rollup.null_nav_count,
        fx_unavailable_count=rollup.fx_unavailable_count,
        allocations=allocations,
        holding_xirr=holding_xirr,
    )


def _source_portfolio_cashflows(
    session: Session, *, user_id: UUID, sourced_ids: list[int]
) -> dict[int, list[tuple[date, int]]]:
    """Per-instrument signed cashflows (**INR paise**) for ``sourced_ids`` — **terminal-free**.

    Shared by :func:`compute_portfolio_summary` (the portfolio XIRR) and
    ``performance_service.compute_portfolio_performance`` (the benchmark replay) so both legs
    run the *identical* INR cashflow set — alpha is only apples-to-apples if they do. The caller
    appends its own terminal cashflow (portfolio current value, or the benchmark counterfactual
    value); this helper never appends one. Returns a ``defaultdict`` so a holding whose only
    rows are unit-only events (``bonus``/``split`` → no cashflow) reads back as ``[]``.

    Each row is converted at *its own* ``fx_rate_to_inr`` (historical clock) **before** the
    sign-preserving aggregation — converting per-row, not on a summed total, because rows may
    carry different rates. An INR row's rate is 1, so the multiply is an exact no-op.
    """
    cashflows: dict[int, list[tuple[date, int]]] = defaultdict(list)
    if not sourced_ids:
        return cashflows
    txns = session.scalars(
        select(InvestmentTransaction)
        .where(
            InvestmentTransaction.user_id == user_id,
            InvestmentTransaction.instrument_id.in_(sourced_ids),
        )
        .order_by(InvestmentTransaction.date, InvestmentTransaction.id)
    )
    for txn in txns:
        signed = _signed_cashflow(txn)
        if signed is not None:
            cashflows[txn.instrument_id].append(
                (txn.date, to_inr_paise(signed, txn.fx_rate_to_inr))
            )
    return cashflows


def _signed_cashflow(txn: InvestmentTransaction) -> int | None:
    """Signed native-paise cashflow for XIRR, or ``None`` for unit-only events.

    Sign is from the investor's perspective: money OUT (acquiring units) is
    negative, money IN (proceeds, payouts) positive. Exhaustive over the 8-member
    transaction-type enum — a future 9th type must fail loud here, not silently drop
    out of the return calc.

    Deliberately **not** driven by ``holdings_service.UNIT_SIGN``: this partitions cash
    direction, that one partitions unit direction, and the two genuinely disagree —
    ``dividend`` is +cash with zero units, ``bonus`` is zero cash with units. Merging
    them would give one of the two the wrong answer.
    """
    t = txn.transaction_type
    if t in ("buy", "sip", "switch_in"):
        return -(txn.amount_native_paise + txn.fees_native_paise)
    if t in ("sell", "switch_out"):
        return txn.amount_native_paise - txn.fees_native_paise
    if t == "dividend":
        return txn.amount_native_paise
    if t in ("bonus", "split"):
        return None  # unit-only events — no cashflow
    raise ValueError(f"unhandled investment transaction_type for XIRR: {t!r}")  # pragma: no cover


def _safe_xirr(
    cashflows: list[tuple[date, int]], *, scope: str, instrument_id: int | None = None
) -> float | None:
    """XIRR of dated signed paise as an annualized fraction, or ``None`` if unsolvable.

    ``pyxirr.xirr(..., silent=True)`` returns ``None`` instead of raising
    ``InvalidPaymentsError`` (cashflows with no sign change — single flow, all-same
    sign, zero terminal), and also returns ``None`` on non-convergence — so one
    ``None`` check covers both. Amounts pass as floats (no ÷100; XIRR is
    scale-invariant). The unsolved warning carries scope + instrument_id only,
    never raw amounts (PII discipline).
    """
    dates = [d for d, _ in cashflows]
    amounts = [float(paise) for _, paise in cashflows]
    result = pyxirr.xirr(dates, amounts, silent=True)
    if result is None:
        logger.warning("portfolio.xirr_unsolved", scope=scope, instrument_id=instrument_id)
        return None
    return float(result)
