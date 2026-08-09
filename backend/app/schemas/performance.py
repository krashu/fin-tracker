"""Portfolio-vs-benchmark performance schema (PRD §F8 view 5).

The response of ``GET /api/v1/portfolio/performance`` — the scalar "am I beating the
market" answer. ``portfolio_xirr`` / ``benchmark_xirr`` / ``alpha`` are annualized
fractions (0.12 = 12%; alpha is the difference in fraction-points, e.g. 0.03 = +3 pp),
``null`` when unsolvable. Money is INR paise. The honesty flags are PRD-required, not
decoration: ``is_fund_post_ter`` (the benchmark is a post-expense *fund*, not the raw
index), ``partial`` (history doesn't cover the earliest cashflow), ``benchmark_cache_stale``
(a cashflow / the terminal fell past the cached NAVs and was clamped), ``is_multi_asset``
(a multi-asset portfolio vs a single-asset index), ``nav_staleness_days`` (the portfolio's
valuation age) + ``null_nav_count``. The FX layer adds two more: ``fx_staleness_days`` (age of
the USD→INR rate used, ``null`` for an all-INR portfolio) and ``fx_unavailable_count`` (USD
holdings priced but with no cached rate — excluded from the number, flagged not hidden). When
``benchmark_xirr`` is ``null``, ``benchmark_unavailable_reason`` says why so the UI shows "—"
with a cause, never a fabricated number.

**The staleness calendar, defined once, here.** ``nav_staleness_days`` and
``fx_staleness_days`` count **calendar** days — plain ``as_of − valuation_date``, no
weekday arithmetic. A valuation is worth warning about at :data:`STALENESS_WARN_DAYS` = 4,
which is a weekend plus one business day and is the smallest threshold that does not fire
on an ordinary Monday: Friday to Monday IS 3 calendar days, so a ``>= 3`` gate flagged
every Indian-MF portfolio every Monday morning, immediately after a refresh that had
nothing left to fetch. No server-side business-day counting: doing it correctly for Indian
MFs needs an exchange-holiday calendar the PRD does not ask for, and without one a Tuesday
after a Monday holiday is wrong anyway. Accepted cost, stated rather than hidden: a
genuinely 3-day-stale NAV mid-week goes unwarned.

Nothing server-side *gates* on the constant — the API reports the raw age and the client
decides — but it lives here, next to the fields it qualifies, so the TS mirror in
``frontend/lib/investments.ts`` can point at one definition instead of re-deriving the
calendar. Re-deriving it in a comment is what produced the off-by-one.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

STALENESS_WARN_DAYS = 4

# Closed set of reasons ``benchmark_xirr`` is ``null``. The frontend switches on these,
# so they're a typed contract — a typo here is a type error, not a silent UI miss.
BenchmarkUnavailableReason = Literal[
    "no_benchmark_data",
    "no_portfolio_cashflows",
    "as_of_before_inception",
    "negative_units",
    "zero_terminal",
    "unsolved",
]


class PortfolioPerformance(BaseModel):
    benchmark_id: int
    benchmark_name: str
    is_fund_post_ter: bool

    portfolio_xirr: float | None
    benchmark_xirr: float | None
    alpha: float | None

    portfolio_value_paise: int
    benchmark_value_paise: int
    value_gap_paise: int

    partial: bool
    benchmark_cache_stale: bool
    is_multi_asset: bool
    nav_staleness_days: int | None
    null_nav_count: int
    fx_staleness_days: int | None
    fx_unavailable_count: int
    benchmark_unavailable_reason: BenchmarkUnavailableReason | None
