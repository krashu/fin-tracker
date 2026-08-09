"""Portfolio-vs-benchmark scalar alpha (PRD §F8 view 5) — the "am I beating the market" number.

Cashflow-matched / PME (public-market-equivalent) counterfactual: take the user's exact
signed investment cashflows — the **same** set the portfolio XIRR uses — and replay them into
a chosen INR index *fund*. Each contribution buys benchmark units at that day's NAV, each
withdrawal (sell, or a genuine cash dividend) sells units. Value the surviving units at the
latest NAV and run the same money-weighted XIRR. ``alpha = portfolio_xirr − benchmark_xirr``;
both legs share one cashflow vector, only the terminal differs — that's what makes alpha
apples-to-apples.

A *reinvested* dividend is a linked ``dividend`` + ``buy`` pair (see ``portfolio_service``).
Both legs share one date, so :func:`_benchmark_leg` prices them at the same
``_nav_on_or_after`` NAV and the benchmark units bought and sold cancel exactly in
full-precision ``Decimal`` — no numerical effect on either leg. It can still shift the
``partial`` / ``cache_stale`` honesty flags, since the pair introduces a cashflow *date* the
NAV cache may not cover; that is the flags working, not a regression.

Key correctness pins (baked in + tested):

* **Both legs source the identical flow set** (``_source_portfolio_cashflows`` — the NAV-bearing
  currently-held holdings). Alpha is "vs the index, on what I still hold", not lifetime PME.
* **Forward pricing** (SEBI): a cashflow is priced at the *next-available* NAV (date ≥ flow);
  one dated past the cache clamps to the latest NAV and flags ``benchmark_cache_stale``. A flow
  before the fund's earliest NAV clamps to inception and flags ``partial`` (never synthesize a
  pre-inception tail).
* **Terminal** = the *latest-available* NAV (date ≤ ``as_of``); if ``as_of`` predates the cache
  there is no priceable terminal → ``benchmark_xirr=None`` with a reason.
* **Decimal discipline**: units accumulate at full precision, rounded once at the terminal
  (``ROUND_HALF_EVEN``, mirroring ``holdings_service``); NAVs load through the ORM column so
  ``PriceNative`` decodes the scaled int.
* **Degenerate guards** *before* the solver: net ``benchmark_units ≤ 0`` (over-sold) or a
  non-positive terminal returns ``None`` + a distinct reason, never a garbage XIRR.
* **Staleness is shown, not hidden** (PRD §Verification §4 + user decision): ``nav_staleness_days``
  + ``null_nav_count`` flag a stale portfolio leg; ``fx_staleness_days`` + ``fx_unavailable_count``
  do the same for the FX layer (a USD holding on a stale / missing rate); alpha is still computed.
  ``nav_staleness_days`` here means the **held** set — the refresh endpoint's
  ``catalogue_staleness_days`` is the same arithmetic over every active instrument and will
  differ; see :func:`_portfolio_nav_staleness`.

Reads only the cached ``benchmark_nav`` — never fetches mfapi (that is the seed-time job of
``benchmark_service``). Multi-currency: both legs run the **INR-normalised** cashflow set
(``_source_portfolio_cashflows`` converts each row at its own ``fx_rate_to_inr``), and the
benchmark NAVs are INR — so alpha is INR-vs-INR. A USD holding with no cached FX rate can't be
priced in INR, so it never enters the sourced set (``fx_unavailable_count`` flags it).
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.log_config import get_logger
from app.models import Benchmark, BenchmarkNav, Instrument
from app.schemas.performance import BenchmarkUnavailableReason, PortfolioPerformance
from app.services.fx_service import latest_rate_date
from app.services.holdings_service import max_staleness_days
from app.services.portfolio_service import (
    _safe_xirr,
    _source_portfolio_cashflows,
    compute_portfolio_summary,
)

logger = get_logger(__name__)

# Cashflow = (date, signed native paise); buys negative, sells/dividends positive.
_Cashflow = tuple[date, int]


def compute_portfolio_performance(
    session: Session, *, user_id: UUID, benchmark_id: int, as_of: date
) -> PortfolioPerformance:
    """Scalar alpha of the user's portfolio vs ``benchmark_id``, valued at ``as_of``.

    The route resolves ``benchmark_id`` (default + 404); this assumes it names an active
    benchmark. ``as_of`` (the route passes ``clock.today()`` — UTC) anchors both terminals.
    """
    benchmark = session.get(Benchmark, benchmark_id)
    if benchmark is None:  # pragma: no cover - route guarantees existence
        raise ValueError(f"benchmark {benchmark_id} not found")

    summary = compute_portfolio_summary(session, user_id=user_id, as_of=as_of)
    # The NAV-bearing held instruments the portfolio XIRR sourced — replay the SAME set.
    sourced_ids = [hx.instrument_id for hx in summary.holding_xirr]

    nav_staleness_days = _portfolio_nav_staleness(
        session, user_id=user_id, sourced_ids=sourced_ids, as_of=as_of
    )
    fx_staleness_days = _fx_staleness(
        session, user_id=user_id, sourced_ids=sourced_ids, as_of=as_of
    )

    per_instrument = _source_portfolio_cashflows(session, user_id=user_id, sourced_ids=sourced_ids)
    union: list[_Cashflow] = [flow for flows in per_instrument.values() for flow in flows]

    nav_rows = session.execute(
        select(BenchmarkNav.nav_date, BenchmarkNav.nav)
        .where(BenchmarkNav.benchmark_id == benchmark_id)
        .order_by(BenchmarkNav.nav_date.asc())
    ).all()
    dates = [r.nav_date for r in nav_rows]
    navs = [r.nav for r in nav_rows]

    value_paise, benchmark_xirr, reason, partial, cache_stale = _benchmark_leg(
        union, dates, navs, as_of
    )

    portfolio_xirr = summary.xirr
    alpha = (
        portfolio_xirr - benchmark_xirr
        if portfolio_xirr is not None and benchmark_xirr is not None
        else None
    )

    logger.info(
        "portfolio_performance_computed",
        benchmark_id=benchmark_id,
        has_alpha=alpha is not None,
        benchmark_unavailable_reason=reason,
        partial=partial,
        benchmark_cache_stale=cache_stale,
        nav_staleness_days=nav_staleness_days,
        fx_staleness_days=fx_staleness_days,
        fx_unavailable_count=summary.fx_unavailable_count,
    )
    return PortfolioPerformance(
        benchmark_id=benchmark_id,
        benchmark_name=benchmark.name,
        is_fund_post_ter=True,
        portfolio_xirr=portfolio_xirr,
        benchmark_xirr=benchmark_xirr,
        alpha=alpha,
        portfolio_value_paise=summary.current_value_paise,
        benchmark_value_paise=value_paise,
        value_gap_paise=summary.current_value_paise - value_paise,
        partial=partial,
        benchmark_cache_stale=cache_stale,
        is_multi_asset=len(summary.allocations) > 1,
        nav_staleness_days=nav_staleness_days,
        null_nav_count=summary.null_nav_count,
        fx_staleness_days=fx_staleness_days,
        fx_unavailable_count=summary.fx_unavailable_count,
        benchmark_unavailable_reason=reason,
    )


def _portfolio_nav_staleness(
    session: Session, *, user_id: UUID, sourced_ids: list[int], as_of: date
) -> int | None:
    """Max valuation age (days) over the **sourced holdings**, or ``None`` if none priced.

    The population is the point. This is the NAV-bearing, currently-held, FX-priceable set
    the portfolio XIRR replayed — what the user is actually warned about.
    ``nav_snapshot_service`` folds the *same* expression
    (:func:`app.services.holdings_service.max_staleness_days`) over the whole instrument
    catalogue and reports it as ``catalogue_staleness_days``; the two legitimately differ
    (an exited-but-unarchived holding ages forever) and used to share this name.

    It re-queries the instruments rather than reusing ``Holding.nav_staleness_days``, which
    now exists: that field covers every *current position*, while ``sourced_ids`` is the
    narrower set the XIRR actually replayed (a priced USD holding with no cached FX rate is
    a holding but not a source). Folding the two would silently widen this number. Keeping
    the query is deliberate, not an oversight to clean up.
    (FX normalisation now happens upstream in ``_source_portfolio_cashflows``
    / the holdings rollup, so the former non-INR guard here is gone — a USD holding
    replays correctly.) ``sourced_ids`` are already user-scoped; ``user_id`` is restated
    for symmetry with the rest of the module so no owned read trusts a derived id set.
    """
    if not sourced_ids:
        return None
    rows = session.execute(
        select(Instrument.nav_updated_at).where(
            Instrument.id.in_(sourced_ids), Instrument.user_id == user_id
        )
    ).all()
    return max_staleness_days((nu for (nu,) in rows), as_of=as_of)


def _fx_staleness(
    session: Session, *, user_id: UUID, sourced_ids: list[int], as_of: date
) -> int | None:
    """Age (days) of the newest cached USD→INR rate vs ``as_of`` — the FX-cache staleness signal.

    ``None`` when the sourced set has no USD holding (FX is irrelevant) or no rate is cached at
    all. Mirrors ``nav_staleness_days``: a USD holding priced off a weeks-old rate is computable
    but worth flagging, exactly like a stale NAV.
    """
    if not sourced_ids:
        return None
    has_usd = session.scalar(
        select(Instrument.id)
        .where(
            Instrument.id.in_(sourced_ids),
            Instrument.user_id == user_id,
            Instrument.currency != "INR",
        )
        .limit(1)
    )
    if has_usd is None:
        return None
    latest = latest_rate_date(session)
    return (as_of - latest).days if latest is not None else None


def _benchmark_leg(
    union: list[_Cashflow], dates: list[date], navs: list[Decimal], as_of: date
) -> tuple[int, float | None, BenchmarkUnavailableReason | None, bool, bool]:
    """Replay ``union`` into the benchmark and value it.

    Returns ``(benchmark_value_paise, benchmark_xirr, unavailable_reason, partial,
    cache_stale)``. ``benchmark_xirr`` is ``None`` with a reason for every degenerate
    case; ``partial`` / ``cache_stale`` are honesty flags that travel even when the
    number IS computed.
    """
    if not dates:
        return 0, None, "no_benchmark_data", False, False
    if not union:
        return 0, None, "no_portfolio_cashflows", False, False

    first_date, last_date = dates[0], dates[-1]
    partial = False
    cache_stale = False
    units = Decimal(0)
    for d, signed_paise in union:
        if d < first_date:
            partial = True  # priced at inception NAV — coverage gap
        if d > last_date:
            cache_stale = True  # priced at latest NAV — stale cache
        nav_d = _nav_on_or_after(dates, navs, d)
        # Sign inverts vs the XIRR flow: a buy (signed_paise < 0) ADDS units.
        units += Decimal(-signed_paise) / Decimal(100) / nav_d

    term_idx = bisect_right(dates, as_of) - 1
    if term_idx < 0:
        # as_of predates the whole cache — no priceable terminal.
        return 0, None, "as_of_before_inception", partial, cache_stale
    if as_of > last_date:
        cache_stale = True
    nav_terminal = navs[term_idx]

    # Degenerate guards BEFORE the solver: a negative terminal can solve to a garbage rate.
    if units <= 0:
        return 0, None, "negative_units", partial, cache_stale
    value_paise = int((units * nav_terminal * Decimal(100)).to_integral_value(ROUND_HALF_EVEN))
    if value_paise <= 0:
        return 0, None, "zero_terminal", partial, cache_stale

    benchmark_xirr = _safe_xirr([*union, (as_of, value_paise)], scope="benchmark")
    reason: BenchmarkUnavailableReason | None = None if benchmark_xirr is not None else "unsolved"
    return value_paise, benchmark_xirr, reason, partial, cache_stale


def _nav_on_or_after(dates: list[date], navs: list[Decimal], d: date) -> Decimal:
    """Next-available NAV (forward pricing): smallest cached date ≥ ``d``.

    A date past the last cached one clamps to the latest NAV; a date before the first
    (pre-inception) falls to ``navs[0]`` via ``bisect_left`` returning 0 (inception clamp).
    """
    idx = bisect_left(dates, d)
    if idx == len(dates):
        return navs[-1]
    return navs[idx]
