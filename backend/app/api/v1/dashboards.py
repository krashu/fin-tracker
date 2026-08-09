"""``/api/v1/dashboards`` — read-only aggregate views (PRD §F8).

v1 ships ``GET /dashboards/spend-by-category`` (PRD §F8 view 2) and
``GET /dashboards/spend-by-period`` (view 3, the weekly/monthly spend bar).
The remaining views (portfolio tiles, net worth over time) depend on F7 / FX
wiring and land later.

Routes here are pure aggregation: signed-sum SQL over the existing
``transactions`` table, scoped to the current user, filtered to the
review-committed board (``confirmed_at IS NOT NULL``). No service layer —
there's no business logic, just an indexed GROUP BY.
"""

from __future__ import annotations

import calendar
import re
from collections import defaultdict
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Annotated, Literal, NamedTuple
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUserId, SessionDep
from app.models import Account, Category, Label, MerchantTagMap, Transaction, TransactionLabel
from app.schemas import (
    NET_WORTH_EXCLUDED_TYPES,
    AccountBalanceRow,
    AvailableYearsResponse,
    CashflowByPeriodBucket,
    CashflowByPeriodResponse,
    OverviewResponse,
    PeriodTotalsResponse,
    SpendByCategoryByPeriodBucket,
    SpendByCategoryByPeriodResponse,
    SpendByCategoryResponse,
    SpendByCategoryRow,
    SpendByPeriodBucket,
    SpendByPeriodResponse,
    SpendByTagByPeriodBucket,
    SpendByTagByPeriodResponse,
    SpendByTagPeriodTotal,
    SpendByTagResponse,
    SpendByTagRow,
    SpendCategoryPeriodTotal,
    SpendCategoryRef,
    TaggingStatsResponse,
    TagRef,
    TopMerchantRow,
    TopMerchantsResponse,
)
from app.services.fx_service import latest_rate
from app.services.holdings_service import compute_holdings, summarize_holdings
from app.services.transaction_queries import confirmed_only

router = APIRouter(prefix="/dashboards", tags=["dashboards"])

# Anchored YYYY-MM with month in [01, 12]. Validated route-side rather than
# via Query(pattern=) so FastAPI's RequestValidationError can't echo the
# rejected input — matches the imports.py:80 input-echo discipline.
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


# ---------- route prologue ----------------------------------------------------
#
# The three shapes every route in this file opened with, hand-copied 4 / 5 / 2 times
# (A1.2/A2.2). Extracted for the reason :448 already gives for `_bucket_of` and
# `_iter_periods`: the next edit to any of them is a one-line diff here instead of a
# four-block sweep that lands on three, after which /overview reports a different
# month than /spend-by-category for the same ?month=.


class MonthWindow(NamedTuple):
    """A validated ``YYYY-MM`` expanded to its calendar bounds."""

    year: int
    mon: int
    first: date
    last: date


def _month_window(month: str) -> MonthWindow:
    """Validate ``YYYY-MM`` and expand it to the calendar month's inclusive bounds.

    422s on a malformed value with a generic detail — the rejected input is never
    echoed back (input-echo discipline; see imports.py:80).

    The boundary is calendar-local: ``Transaction.date`` is a naive calendar date and
    ``confirmed_at`` (UTC) is **not** consulted, so a row dated ``2026-05-31`` lands in
    the May bucket regardless of its ``confirmed_at`` instant. No route defaults to
    "current month" — the user's timezone is the frontend's concern, so ``month`` is
    always required.

    ``year`` rides along with ``first`` / ``last`` because :func:`overview` derives its
    calendar-YTD floor from it. The window derives strictly from ``month``, never
    ``date.today()`` (ADR-0001 rule 5) — returning only the bounds would push that route
    back to re-parsing the string, which is how that guarantee gets lost.
    """
    if not _MONTH_RE.match(month):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="month must match YYYY-MM",
        )
    year, mon = int(month[:4]), int(month[5:7])
    return MonthWindow(
        year=year,
        mon=mon,
        first=date(year, mon, 1),
        last=date(year, mon, calendar.monthrange(year, mon)[1]),
    )
_YEAR_RE = re.compile(r"^\d{4}$")


class PeriodBounds(NamedTuple):
    first: date
    last: date
    month: str | None
    year: str | None


def _parse_period_window(month: str | None, year: str | None) -> PeriodBounds:
    """Parse either month (YYYY-MM) or year (YYYY) into inclusive calendar bounds."""
    if year is not None:
        if not _YEAR_RE.match(year):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="year must match YYYY",
            )
        y = int(year)
        return PeriodBounds(
            first=date(y, 1, 1),
            last=date(y, 12, 31),
            month=None,
            year=year,
        )
    elif month is not None:
        win = _month_window(month)
        return PeriodBounds(
            first=win.first,
            last=win.last,
            month=month,
            year=None,
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="month or year is required",
        )


@router.get("/available-years", response_model=AvailableYearsResponse)
def available_years(
    session: SessionDep,
    user_id: CurrentUserId,
) -> AvailableYearsResponse:
    """Return distinct calendar years with confirmed transactions up to current year."""
    min_date = session.scalar(
        confirmed_only(
            select(func.min(Transaction.date)).where(Transaction.user_id == user_id)
        )
    )
    current_year = date.today().year
    if not min_date:
        return AvailableYearsResponse(years=[current_year])

    start_year = min_date.year
    end_year = max(current_year, date.today().year)
    years = list(range(end_year, start_year - 1, -1))
    return AvailableYearsResponse(years=years)



def _require_ordered(start: date, end: date) -> None:
    """422 when an explicit date range is inverted. The values are never echoed.

    No max-window guard: single-user / local-first v1 with a frontend-controlled range
    (CLAUDE.md §2, no speculative limits). A ceiling at v2 — when this is multi-user or
    network-exposed — is one added line here rather than five.
    """
    if start > end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="start must be on or before end",
        )


class IncomeExpense(NamedTuple):
    """Signed totals over a date range. ``expense_paise`` is Σ(spend, refund)."""

    income_paise: int
    expense_paise: int


def _income_expense_sums(
    session: Session, *, user_id: UUID, start: date, end: date
) -> IncomeExpense:
    """Signed income / expense totals over the inclusive ``[start, end]``, confirmed only.

    ``expense_paise`` is the signed Σ(spend, refund): negative on an ordinary window and
    legitimately **positive** on a refund-dominant one, so callers must not clamp it.
    ``transfer`` is excluded — intra-account movement is neither income nor spend.
    Server-computed so no client can drift on the sign.

    Returns a NamedTuple rather than a bare ``tuple[int, int]``: two same-typed
    signed-paise values in positional order is a silent-swap hazard that would invert
    income and expense with no type error to catch it.
    """
    expense_sum = func.sum(
        case(
            (
                Transaction.transaction_type.in_(("spend", "refund")),
                Transaction.amount_paise,
            ),
            else_=0,
        )
    )
    income_sum = func.sum(
        case((Transaction.transaction_type == "income", Transaction.amount_paise), else_=0)
    )
    stmt = select(expense_sum, income_sum).where(
        Transaction.user_id == user_id,
        Transaction.transaction_type.in_(("spend", "refund", "income")),
        Transaction.date >= start,
        Transaction.date <= end,
    )
    stmt = confirmed_only(stmt)

    expense_raw, income_raw = session.execute(stmt).one()
    return IncomeExpense(income_paise=int(income_raw or 0), expense_paise=int(expense_raw or 0))


@router.get("/spend-by-category", response_model=SpendByCategoryResponse)
def spend_by_category(
    session: SessionDep,
    user_id: CurrentUserId,
    month: Annotated[str | None, Query()] = None,
    year: Annotated[str | None, Query()] = None,
    label_id: Annotated[int | None, Query(gt=0)] = None,
) -> SpendByCategoryResponse:
    """Signed-sum spend per category for the given calendar month or year."""
    bounds = _parse_period_window(month, year)
    first, last = bounds.first, bounds.last

    total_paise = func.sum(Transaction.amount_paise).label("total_paise")

    # LEFT JOIN keeps uncategorized rows (category_id IS NULL) in the result;
    # the Category.user_id == user_id predicate on the JOIN itself (not the
    # outer WHERE) is belt-and-suspenders: today every write-site funnels
    # through _assert_category_id_or_422 so a Transaction cannot reference a
    # foreign-user Category, but the FK alone is cross-user-permissive. A
    # future code path that bypassed the helper would silently leak another
    # user's category *name* into this user's dashboard without this clause.
    # No LIMIT: the row count is bounded by the user's distinct in-window
    # categories (~15 seeded defaults + a handful of custom = tens, not
    # thousands) plus one uncategorized row. The type filter above keeps a
    # regression that widens the set from ballooning the response. If a
    # future change drops the GROUP BY (e.g. per-transaction drilldown moves
    # here), reinstate a LIMIT.
    stmt = (
        select(
            Transaction.category_id,
            Category.name.label("category_name"),
            total_paise,
        )
        .outerjoin(
            Category,
            and_(
                Category.id == Transaction.category_id,
                Category.user_id == user_id,
            ),
        )
        .where(
            Transaction.user_id == user_id,
            Transaction.transaction_type.in_(("spend", "refund")),
            Transaction.date >= first,
            Transaction.date <= last,
        )
        .group_by(Transaction.category_id, Category.name)
        # Portable NULL-last sort: boolean key first (False < True puts
        # categorized rows above the uncategorized bucket), then most-
        # negative-sum first within the categorized block, then category_id
        # as a stable tiebreaker for equal totals (deterministic output
        # across runs / dialects). Avoids nulls_last() (dialect-specific on
        # SQLite < 3.30).
        .order_by(
            Transaction.category_id.is_(None),
            total_paise.asc(),
            Transaction.category_id.asc(),
        )
    )
    # Board rows only (confirmed_at IS NOT NULL) — shared with GET /transactions.
    stmt = confirmed_only(stmt)
    # Optional F3a tag filter — EXISTS subquery (one link per (txn, label)), so no
    # join-row duplication; a txn either carries the label or doesn't. Copies the
    # transactions.py list-route idiom verbatim.
    if label_id is not None:
        stmt = stmt.where(Transaction.labels.any(Label.id == label_id))

    rows = [
        SpendByCategoryRow(
            category_id=cat_id,
            category_name=cat_name,
            total_paise=int(total),
        )
        for cat_id, cat_name, total in session.execute(stmt).all()
    ]
    return SpendByCategoryResponse(month=bounds.month, year=bounds.year, rows=rows, label_id=label_id)


@router.get("/spend-by-tag", response_model=SpendByTagResponse)
def spend_by_tag(
    session: SessionDep,
    user_id: CurrentUserId,
    month: Annotated[str | None, Query()] = None,
    year: Annotated[str | None, Query()] = None,
) -> SpendByTagResponse:
    """Signed-sum spend per tag for the given calendar month or year."""
    bounds = _parse_period_window(month, year)
    first, last = bounds.first, bounds.last

    tag_total = func.sum(Transaction.amount_paise).label("total_paise")
    # Per-tag grouped sums — arc decision #7's group-by shape, which INTENTIONALLY
    # double-counts a multi-tagged txn across its tags. ``select_from(Transaction)``
    # anchors the FROM explicitly (the selected columns are Label's; only the sum
    # is Transaction's). ``Label.user_id == user_id`` on the JOIN mirrors
    # spend_by_category's cross-user-safe Category join — a foreign-user label can
    # never surface its name here. No LIMIT: bounded by the user's distinct
    # in-window labels (tens, not thousands).
    grouped = (
        select(Label.id, Label.name, tag_total)
        .select_from(Transaction)
        .join(
            TransactionLabel,
            and_(
                TransactionLabel.transaction_id == Transaction.id,
                TransactionLabel.user_id == Transaction.user_id,
            ),
        )
        .join(
            Label,
            and_(Label.id == TransactionLabel.label_id, Label.user_id == user_id),
        )
        .where(
            Transaction.user_id == user_id,
            Transaction.transaction_type.in_(("spend", "refund")),
            Transaction.date >= first,
            Transaction.date <= last,
        )
        .group_by(Label.id, Label.name)
        # Most-negative (biggest spend) first; label id asc is a stable tiebreak
        # for equal totals (deterministic across runs / dialects).
        .order_by(tag_total.asc(), Label.id.asc())
    )
    grouped = confirmed_only(grouped)
    rows = [
        SpendByTagRow(label_id=lid, label_name=name, total_paise=int(total))
        for lid, name, total in session.execute(grouped).all()
    ]

    # Two scalar sums over the same month filter (no tag join → no double-count):
    # the honest total (coverage denominator) and the untagged complement.
    month_where = [
        Transaction.user_id == user_id,
        Transaction.transaction_type.in_(("spend", "refund")),
        Transaction.date >= first,
        Transaction.date <= last,
    ]
    total_stmt = confirmed_only(select(func.sum(Transaction.amount_paise)).where(*month_where))
    total_spend_paise = int(session.scalar(total_stmt) or 0)

    # Untagged bucket = txns carrying zero labels (NOT EXISTS). It is both the
    # bottom-pinned breakdown row (arc decision #4) and the coverage complement.
    untagged_stmt = confirmed_only(
        select(func.sum(Transaction.amount_paise)).where(*month_where, ~Transaction.labels.any())
    )
    untagged_paise = int(session.scalar(untagged_stmt) or 0)
    if untagged_paise != 0:
        # Appended last → pinned after every real tag regardless of magnitude
        # (the frontend's sign-split preserves order within each sign group).
        rows.append(SpendByTagRow(label_id=None, label_name=None, total_paise=untagged_paise))

    # tagged = total − untagged (signed partition identity: every txn is tagged
    # xor untagged). Rate only when it lands cleanly in [0, 1]; else None (no
    # spend, or refund-skew pushes the signed ratio out of range). Never clamp the
    # signed building blocks (arc decision #6). ``+ 0.0`` normalises the signed
    # zero an all-untagged month produces (0 / negative-spend = ``-0.0``, which
    # serialises as ``-0.0`` and renders as "-0%"); the range guard below can't
    # see it, since ``-0.0 == 0.0``. IEEE-754 leaves every other value unchanged.
    tagged_paise = total_spend_paise - untagged_paise
    rate = ((tagged_paise / total_spend_paise) + 0.0) if total_spend_paise else None
    coverage_rate = rate if (rate is not None and 0.0 <= rate <= 1.0) else None

    return SpendByTagResponse(
        month=bounds.month,
        year=bounds.year,
        rows=rows,
        total_spend_paise=total_spend_paise,
        tagged_paise=tagged_paise,
        coverage_rate=coverage_rate,
    )


@router.get("/top-merchants", response_model=TopMerchantsResponse)
def top_merchants(
    session: SessionDep,
    user_id: CurrentUserId,
    month: Annotated[str | None, Query()] = None,
    year: Annotated[str | None, Query()] = None,
    label_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 5,
) -> TopMerchantsResponse:
    """Biggest-spender merchants for the given calendar month or year."""
    bounds = _parse_period_window(month, year)
    first, last = bounds.first, bounds.last

    total_paise = func.sum(Transaction.amount_paise).label("total_paise")
    merchant_label = func.coalesce(
        func.max(Transaction.merchant_raw), Transaction.merchant_normalized
    ).label("merchant_label")

    # Shared WHERE for the grouped list and the distinct-count; merchant_normalized
    # != "" drops the no-merchant bucket from both, so total_merchants counts only
    # merchants that can actually appear in `rows`. A list (not a tuple) so the
    # optional F3a tag filter appends onto BOTH queries — keeping total_merchants
    # (hence "top N of M") scoped to the same tagged set as the rows.
    base_where = [
        Transaction.user_id == user_id,
        Transaction.transaction_type.in_(("spend", "refund")),
        Transaction.date >= first,
        Transaction.date <= last,
        Transaction.merchant_normalized != "",
    ]
    if label_id is not None:
        # EXISTS subquery (see spend_by_category / transactions.py) — no join-row
        # duplication of the grouped sum or the distinct count.
        base_where.append(Transaction.labels.any(Label.id == label_id))

    stmt = (
        select(Transaction.merchant_normalized, merchant_label, total_paise)
        .where(*base_where)
        .group_by(Transaction.merchant_normalized)
        # Most-negative (biggest spend) first; normalized key asc is a stable
        # tiebreak for equal totals (deterministic across runs / dialects).
        .order_by(total_paise.asc(), Transaction.merchant_normalized.asc())
        .limit(limit)
    )
    stmt = confirmed_only(stmt)
    rows = [
        TopMerchantRow(
            merchant_normalized=mn,
            merchant_label=label,
            total_paise=int(total),
        )
        for mn, label, total in session.execute(stmt).all()
    ]

    count_stmt = confirmed_only(
        select(func.count(func.distinct(Transaction.merchant_normalized))).where(*base_where)
    )
    total_merchants = int(session.scalar(count_stmt) or 0)

    return TopMerchantsResponse(
        month=bounds.month,
        year=bounds.year,
        rows=rows,
        total_merchants=total_merchants,
        truncated=total_merchants > limit,
        label_id=label_id,
    )


class _Bucket(NamedTuple):
    """The period a date falls in: its label and its last calendar day.

    ``end`` is carried only to step :func:`_iter_periods` forward to the next
    bucket; the bucket's *start* isn't needed under clip semantics (rows are
    bucketed by ``period``, never by a start boundary), so it isn't carried.
    """

    period: str  # "YYYY-MM" (month) | "YYYY-Www" (ISO week)
    end: date


def _bucket_of(d: date, bucket: str) -> _Bucket:
    """Bucket containing ``d``. The single source of truth for the period
    label, so row-bucketing and zero-fill enumeration can't drift apart.

    Week is ISO-8601 (Monday start). The label takes the **ISO year** from
    ``isocalendar()`` so a late-December date in ISO week 1 of the next year
    labels correctly (e.g. 2025-12-30 -> "2026-W01").
    """
    if bucket == "month":
        last = d.replace(day=calendar.monthrange(d.year, d.month)[1])
        return _Bucket(f"{d.year:04d}-{d.month:02d}", last)
    iso = d.isocalendar()  # (year, week, weekday)
    monday = d - timedelta(days=d.weekday())  # weekday(): Mon=0
    return _Bucket(f"{iso.year:04d}-W{iso.week:02d}", monday + timedelta(days=6))


def _iter_periods(window_start: date, window_end: date, bucket: str) -> Iterator[str]:
    """Period labels for every bucket overlapping ``[window_start, window_end]``,
    chronological. Drives zero-fill; routes each step through :func:`_bucket_of`
    so its keys match row-bucketing. Terminates — ``cur`` strictly increases.
    """
    cur = window_start
    while cur <= window_end:
        b = _bucket_of(cur, bucket)
        yield b.period
        cur = b.end + timedelta(days=1)


@router.get("/spend-by-period", response_model=SpendByPeriodResponse)
def spend_by_period(
    session: SessionDep,
    user_id: CurrentUserId,
    bucket: Annotated[Literal["week", "month"], Query()],
    start: Annotated[date, Query()],
    end: Annotated[date, Query()],
    label_id: Annotated[int | None, Query(gt=0)] = None,
) -> SpendByPeriodResponse:
    """Signed-sum spend per ISO-week or calendar-month bucket over [start, end].

    PRD §F8 view 3 (the weekly/monthly spend bar). The window is **clipped**,
    not snapped: it means literally ``[start, end]``, so an edge bucket sums
    only its in-window days (``end = today`` yields a period-to-date bar). The
    frontend owns "last N periods" — it computes the window from today + N and
    passes aligned dates. Buckets with no in-window spend are zero-filled, so
    the bar chart has no gaps.

    Type filter ``("spend", "refund")``, board-only (``confirmed_at``), and the
    INR-only signed-sum convention are identical to ``spend_by_category``;
    ``income`` and ``transfer`` are excluded for the same reasons.

    Bucketing is done in Python, not SQL: a portable week/month GROUP BY
    doesn't exist (SQLite's ``strftime('%W')`` isn't ISO week; Postgres wants
    ``date_trunc``), and this is the first time-bucketing — no second consumer
    to justify a dialect-aware abstraction (CLAUDE.md §2). The SQL side keeps
    only the portable, index-backed date-window filter; the reduce is
    microseconds at single-user scale. ``date_trunc`` is the v2 path (Postgres
    + volume = the second concrete use).
    """
    _require_ordered(start, end)
    # No max-window guard: single-user / local-first v1, frontend-controlled
    # range (CLAUDE.md §2, no speculative limits). Add a ceiling at v2 when
    # this is multi-user / network-exposed.

    stmt = select(Transaction.date, Transaction.amount_paise).where(
        Transaction.user_id == user_id,
        Transaction.transaction_type.in_(("spend", "refund")),
        Transaction.date >= start,
        Transaction.date <= end,
    )
    # Board rows only (confirmed_at IS NOT NULL) — shared with spend_by_category.
    stmt = confirmed_only(stmt)
    # Optional F3a tag filter — EXISTS subquery (see spend_by_category).
    if label_id is not None:
        stmt = stmt.where(Transaction.labels.any(Label.id == label_id))

    totals: dict[str, int] = defaultdict(int)
    for d, amt in session.execute(stmt).all():
        # Python-sum path stays int (unlike spend_by_category's int(func.sum)
        # over SQL) — intentionally cast-free; don't "add the missing int()"
        # or port to func.sum without re-checking the money type.
        totals[_bucket_of(d, bucket).period] += amt

    buckets = [
        # totals.get(p, 0) is load-bearing: zero-filled periods are enumerated
        # here but never written to `totals` — don't "simplify" to totals[p].
        SpendByPeriodBucket(period=p, total_paise=totals.get(p, 0))
        for p in _iter_periods(start, end, bucket)
    ]
    return SpendByPeriodResponse(
        bucket=bucket, start=start, end=end, buckets=buckets, label_id=label_id
    )


@router.get("/cashflow-by-period", response_model=CashflowByPeriodResponse)
def cashflow_by_period(
    session: SessionDep,
    user_id: CurrentUserId,
    bucket: Annotated[Literal["week", "month"], Query()],
    start: Annotated[date, Query()],
    end: Annotated[date, Query()],
) -> CashflowByPeriodResponse:
    """Income vs spend + net per ISO-week or calendar-month bucket over [start, end].

    The series form of ``period-totals`` (which is single-window): the /spending
    "am I solvent" chart (PRD §F8 view 3). Window is **clipped** (means literally
    ``[start, end]``), buckets are zero-filled, and the frontend owns "last N
    periods" — all identical to ``spend_by_period``. The reduce is Python-side for
    the same portability reason (no dialect-neutral ISO-week GROUP BY).

    Type filter ``("spend", "refund", "income")`` and board-only
    (``confirmed_at``) match ``period_totals``: ``income_paise`` = Σ income (≥ 0);
    ``expense_paise`` = **signed** Σ(spend, refund) (≤ 0 in the common case but
    legitimately > 0 in a refund-dominant bucket — never clamped, or the
    ``net = income + expense`` identity breaks); ``net_paise`` = income + expense
    (server-computed so the client can't drift on the sign, and negative on a
    deficit bucket — the whole point of the chart). ``transfer`` is excluded.
    """
    _require_ordered(start, end)

    stmt = select(
        Transaction.date,
        Transaction.amount_paise,
        Transaction.transaction_type,
    ).where(
        Transaction.user_id == user_id,
        Transaction.transaction_type.in_(("spend", "refund", "income")),
        Transaction.date >= start,
        Transaction.date <= end,
    )
    # Board rows only (confirmed_at IS NOT NULL) — shared with spend_by_period.
    stmt = confirmed_only(stmt)

    # Two accumulators keyed by bucket period. int throughout (money type); the
    # explicit elif (not a catch-all else) keeps expense clean if the SQL type
    # filter above is ever widened.
    income_totals: dict[str, int] = defaultdict(int)
    expense_totals: dict[str, int] = defaultdict(int)
    for d, amt, ttype in session.execute(stmt).all():
        period = _bucket_of(d, bucket).period
        if ttype == "income":
            income_totals[period] += amt
        elif ttype in ("spend", "refund"):
            expense_totals[period] += amt

    buckets = [
        # .get(p, 0) is load-bearing: zero-filled periods are enumerated here but
        # never written to the accumulators — don't "simplify" to [p].
        CashflowByPeriodBucket(
            period=p,
            income_paise=income_totals.get(p, 0),
            expense_paise=expense_totals.get(p, 0),
            net_paise=income_totals.get(p, 0) + expense_totals.get(p, 0),
        )
        for p in _iter_periods(start, end, bucket)
    ]
    return CashflowByPeriodResponse(bucket=bucket, start=start, end=end, buckets=buckets)


@router.get(
    "/spend-by-category-by-period",
    response_model=SpendByCategoryByPeriodResponse,
)
def spend_by_category_by_period(
    session: SessionDep,
    user_id: CurrentUserId,
    bucket: Annotated[Literal["week", "month"], Query()],
    start: Annotated[date, Query()],
    end: Annotated[date, Query()],
    label_id: Annotated[int | None, Query(gt=0)] = None,
) -> SpendByCategoryByPeriodResponse:
    """Signed-sum spend **per category per bucket** over [start, end] — the
    /spending category-trend bar (PRD §F8 view 3, "how is my category mix
    shifting?"). One category at a time, picked from a dropdown; NOT a stack.

    The category×time generalization of ``spend_by_category``: same LEFT JOIN
    Category (with the cross-user-safe ``Category.user_id == user_id`` predicate
    on the join, guarding the foreign-category name leak), same
    ``("spend", "refund")`` type filter, same board-only (``confirmed_at``) and
    INR-only signed-sum convention. Window / bucketing / zero-fill are identical
    to ``spend_by_period`` / ``cashflow_by_period`` (clipped ``[start, end]``,
    Python-side ``_bucket_of`` / ``_iter_periods`` for portability, ``income`` and
    ``transfer`` excluded).

    ``totals`` per bucket is **dense** — a cell for every category in the echoed
    ``categories`` set, zero-filled — so the response is a gap-free category×period
    grid. Each cell's ``total_paise`` is signed and **never clamped** (a net-credit
    category surfaces positive), so ``Σ`` of a bucket's cells reconciles exactly to
    that bucket's ``spend_by_period`` total. The frontend does not clamp either — its
    one consumer renders the signed magnitude (see the schema docstring).
    """
    _require_ordered(start, end)

    # LEFT JOIN + join-side user predicate mirror spend_by_category exactly (see
    # its docstring for the cross-user name-leak rationale).
    stmt = (
        select(
            Transaction.date,
            Transaction.category_id,
            Category.name.label("category_name"),
            Transaction.amount_paise,
        )
        .outerjoin(
            Category,
            and_(
                Category.id == Transaction.category_id,
                Category.user_id == user_id,
            ),
        )
        .where(
            Transaction.user_id == user_id,
            Transaction.transaction_type.in_(("spend", "refund")),
            Transaction.date >= start,
            Transaction.date <= end,
        )
    )
    # Board rows only (confirmed_at IS NOT NULL) — shared with spend_by_category.
    stmt = confirmed_only(stmt)
    # Optional F3a tag filter — EXISTS subquery (see spend_by_category). Independent
    # of the Category outerjoin above; narrows to tagged txns only.
    if label_id is not None:
        stmt = stmt.where(Transaction.labels.any(Label.id == label_id))

    # Flat accumulators (int money type; no nested defaultdict — keeps ty clean
    # and matches cashflow_by_period's flat reduce):
    #   cell_totals[(period, category_id)] -> signed Σ for that grid cell,
    #   grand[category_id]               -> signed Σ across all buckets (stack order),
    #   names[category_id]               -> the joined display name (id-stable).
    cell_totals: dict[tuple[str, int | None], int] = defaultdict(int)
    grand: dict[int | None, int] = defaultdict(int)
    names: dict[int | None, str | None] = {}
    for d, cat_id, cat_name, amt in session.execute(stmt).all():
        period = _bucket_of(d, bucket).period
        cell_totals[(period, cat_id)] += amt
        grand[cat_id] += amt
        names[cat_id] = cat_name

    # Stable stack order = spend_by_category's portable sort, applied to the grand
    # totals: categorized first (is None False < True), most-negative first
    # (biggest overall spender at the bottom of the stack), category_id asc
    # tiebreak; uncategorized (None) pinned last.
    ordered = sorted(
        grand,
        key=lambda cid: (cid is None, grand[cid], cid if cid is not None else 0),
    )
    categories = [
        SpendCategoryRef(category_id=cid, category_name=names.get(cid)) for cid in ordered
    ]

    buckets = [
        SpendByCategoryByPeriodBucket(
            period=p,
            totals=[
                # .get((p, cid), 0) is load-bearing: zero-filled cells (a category
                # with no activity in period p, or a fully zero-filled period) are
                # enumerated here but never written to cell_totals — don't
                # "simplify" to a direct index.
                SpendCategoryPeriodTotal(category_id=cid, total_paise=cell_totals.get((p, cid), 0))
                for cid in ordered
            ],
        )
        for p in _iter_periods(start, end, bucket)
    ]
    return SpendByCategoryByPeriodResponse(
        bucket=bucket,
        start=start,
        end=end,
        categories=categories,
        buckets=buckets,
        label_id=label_id,
    )


@router.get(
    "/spend-by-tag-by-period",
    response_model=SpendByTagByPeriodResponse,
)
def spend_by_tag_by_period(
    session: SessionDep,
    user_id: CurrentUserId,
    bucket: Annotated[Literal["week", "month"], Query()],
    start: Annotated[date, Query()],
    end: Annotated[date, Query()],
) -> SpendByTagByPeriodResponse:
    """Signed-sum spend **per tag per bucket** over [start, end] — the /spending
    tag-trend multi-line chart (PRD §F3a labels; tag-analysis arc Phase C: "is
    spend under a tag growing over time?").

    The tag×time generalization of ``spend_by_tag`` (which is single-month), and
    the tag analog of ``spend_by_category_by_period``. It combines two shapes that
    already exist: the group-by-tag JOIN from ``spend_by_tag`` (arc decision #7 —
    the INNER JOIN over ``transaction_labels`` + ``Label``, which INTENTIONALLY
    double-counts a multi-tagged txn across its tags) and the clipped-window
    Python-side ``_bucket_of`` / ``_iter_periods`` bucketing + zero-fill from
    ``spend_by_period`` / ``spend_by_category_by_period``. Window / type filter
    (``("spend", "refund")``) / board-only (``confirmed_at``) / INR-only signed-int
    convention are all identical to those routes (``income`` and ``transfer``
    excluded).

    Two things differ from ``spend_by_category_by_period``, both flowing from tags
    being many:many where category is 1:1:

    1. **The untagged residual is excluded.** The INNER JOIN keeps only txns that
       carry ≥ 1 label — there is no null-id "untagged" line (arc decision: the
       trend is about tags; the untagged share would dominate the scale, and
       coverage already lives on the Phase-B ``spend_by_tag`` card). Contrast the
       LEFT JOIN in the category route, which keeps the uncategorized bucket.
    2. **No cross-tag reconciliation identity.** Because the cells double-count and
       untagged is dropped, ``Σ`` of a bucket's cells does **not** equal that
       bucket's ``spend_by_period`` total (the category route *does* have that
       identity). The only valid reconciliation is per-tag: a tag's cells over the
       window sum to its ``spend_by_tag`` grouped total for the same window. The
       cells are signed and **never clamped** (arc decision #6).

    Bucketing is Python-side for the same portability reason as the sibling period
    routes (no dialect-neutral ISO-week GROUP BY); the tag JOIN multiplies rows,
    but at single-user scale the reduce is microseconds.
    """
    _require_ordered(start, end)

    # INNER JOIN transaction_labels + Label — the group-by-tag shape from
    # spend_by_tag (arc decision #7). Same-user composite join on transaction_labels
    # (ADR-0002) + Label.user_id == user_id (cross-user name-leak guard). INNER
    # (not outer) drops zero-label txns → the untagged residual is excluded by
    # construction (see docstring). A multi-tagged txn yields one row per tag →
    # intentional double-count.
    stmt = (
        select(
            Transaction.date,
            Label.id,
            Label.name,
            Transaction.amount_paise,
        )
        .select_from(Transaction)
        .join(
            TransactionLabel,
            and_(
                TransactionLabel.transaction_id == Transaction.id,
                TransactionLabel.user_id == Transaction.user_id,
            ),
        )
        .join(
            Label,
            and_(Label.id == TransactionLabel.label_id, Label.user_id == user_id),
        )
        .where(
            Transaction.user_id == user_id,
            Transaction.transaction_type.in_(("spend", "refund")),
            Transaction.date >= start,
            Transaction.date <= end,
        )
    )
    # Board rows only (confirmed_at IS NOT NULL) — shared with the sibling routes.
    stmt = confirmed_only(stmt)

    # Flat accumulators (int money type; mirrors spend_by_category_by_period's flat
    # reduce). label_id is non-null here (INNER JOIN + no untagged bucket), so the
    # key types are plain int — no `int | None` like the category route.
    #   cell_totals[(period, label_id)] -> signed Σ for that grid cell,
    #   grand[label_id]                 -> signed Σ across all buckets (line order),
    #   names[label_id]                 -> the joined label name (id-stable).
    cell_totals: dict[tuple[str, int], int] = defaultdict(int)
    grand: dict[int, int] = defaultdict(int)
    names: dict[int, str] = {}
    for d, label_id, label_name, amt in session.execute(stmt).all():
        period = _bucket_of(d, bucket).period
        cell_totals[(period, label_id)] += amt
        grand[label_id] += amt
        names[label_id] = label_name

    # Stable line order = spend_by_tag's sort applied to the grand totals: most-
    # negative (biggest overall spender) first, label_id asc as a deterministic
    # tiebreak. No uncategorized-style pin (untagged is excluded).
    ordered = sorted(grand, key=lambda lid: (grand[lid], lid))
    tags = [TagRef(label_id=lid, label_name=names[lid]) for lid in ordered]

    buckets = [
        SpendByTagByPeriodBucket(
            period=p,
            totals=[
                # .get((p, lid), 0) is load-bearing: zero-filled cells (a tag with
                # no activity in period p, or a fully zero-filled period) are
                # enumerated here but never written to cell_totals — don't
                # "simplify" to a direct index.
                SpendByTagPeriodTotal(label_id=lid, total_paise=cell_totals.get((p, lid), 0))
                for lid in ordered
            ],
        )
        for p in _iter_periods(start, end, bucket)
    ]
    return SpendByTagByPeriodResponse(
        bucket=bucket,
        start=start,
        end=end,
        tags=tags,
        buckets=buckets,
    )


@router.get("/period-totals", response_model=PeriodTotalsResponse)
def period_totals(
    session: SessionDep,
    user_id: CurrentUserId,
    start: Annotated[date, Query()],
    end: Annotated[date, Query()],
) -> PeriodTotalsResponse:
    """Income vs spend over ``[start, end]`` — the /expenses summary strip's
    income figure (its spend total already comes from ``spend-by-period``).

    Board-only signed sums in one pass: ``expense`` = Σ(spend, refund) (signed,
    refunds net against spend — same rule as the spend aggregates); ``income``
    = Σ(income); ``net`` = income + expense (computed server-side so the client
    can't drift on the sign). ``transfer`` is excluded — intra-account movement
    is neither income nor spend (consistent with the other dashboard routes).
    """
    _require_ordered(start, end)

    income_paise, expense_paise = _income_expense_sums(
        session, user_id=user_id, start=start, end=end
    )
    return PeriodTotalsResponse(
        start=start,
        end=end,
        income_paise=income_paise,
        expense_paise=expense_paise,
        net_paise=income_paise + expense_paise,
    )


@router.get("/overview", response_model=OverviewResponse)
def overview(
    session: SessionDep,
    user_id: CurrentUserId,
    month: Annotated[str, Query()],
) -> OverviewResponse:
    """Financial Overview home aggregate (PRD §F8 view 1 + view 4).

    One call backs the /dashboard landing: per-account current balances, net
    worth, current portfolio value, and the requested month's income / expense
    / net.

    **Balances** are board-only (``confirmed_at IS NOT NULL``) signed sums added
    to each account's ``opening_balance_paise``. Archived accounts are **not**
    filtered out — a closed account that still holds a balance is still part of
    net worth, and dropping it would make archiving silently change the figure
    (the frontend dims/groups archived rows instead). The balances query does
    not join ``accounts``, so this is independent of the spend aggregates'
    archived-account handling.

    **net_worth** = Σ(contributing account balances, signed) + portfolio value,
    where "contributing" is every type not in
    :data:`~app.schemas.dashboards.NET_WORTH_EXCLUDED_TYPES` — today ``bank`` and
    ``cash``, which contribute their signed balance, so a bank overdraft
    legitimately reduces net worth. Two types are **excluded**, for opposite
    reasons. Credit cards are spend channels, not liabilities: bill payments
    aren't recorded, so a CC ``balance_paise`` is lifetime accumulated spend
    rather than real outstanding debt, and counting it (even clamped) would
    understate net worth without bound; each CC row instead carries
    ``spend_ytd_paise`` (calendar year-to-date spend). Investment accounts are
    placeholders — holdings live in ``instruments`` / ``investment_transactions``,
    so an opening balance there would double-count the same money once itemised
    (``AccountCreate`` pins it to 0; rows predating that rule are inert here).
    **Portfolio value** = the INR
    rollup (``current_value_paise``) over holdings with a NAV (null-NAV holdings
    count as 0, never their cost — same rule as /holdings). USD holdings convert
    at today's cached USD→INR rate; a priced USD holding with no cached rate is
    FX-unavailable — excluded from the rollup and surfaced via
    ``fx_unavailable_count`` (cf. /holdings, /portfolio), never silently dropped.

    **income / expense / net** come from the same :func:`_income_expense_sums` helper
    ``period-totals`` calls, over this month's window — one implementation, so the two
    endpoints cannot report different totals for the same month. ``expense`` is the
    signed Σ(spend, refund) ≤ 0, ``net`` = income + expense. ``transfer`` is excluded
    everywhere.
    """
    window = _month_window(month)
    first, last = window.first, window.last

    # Per-account board-only signed sum. No accounts JOIN and no archived_at
    # filter — archived accounts with activity still count (see docstring).
    balance_stmt = (
        select(Transaction.account_id, func.sum(Transaction.amount_paise))
        .where(Transaction.user_id == user_id)
        .group_by(Transaction.account_id)
    )
    balance_stmt = confirmed_only(balance_stmt)
    summed: dict[int, int] = {
        acct_id: int(total or 0) for acct_id, total in session.execute(balance_stmt).all()
    }

    # Per-CC calendar year-to-date spend: signed net Σ(spend, refund) over
    # [Jan 1 of the requested month's year, end of the requested month]. Window
    # derives strictly from `month` (`window.year` / `window.last`, both parsed by
    # _month_window), never date.today() — the route stays deterministic on its input.
    # Signed and never clamped here (a refund-dominant window is legitimately
    # positive; the frontend floors to a non-negative "spent" magnitude). Only
    # credit-card accounts get a value; every other type maps to None below.
    ytd_first = date(window.year, 1, 1)
    ytd_stmt = (
        select(
            Transaction.account_id,
            func.sum(case((Transaction.transaction_type == "spend", Transaction.amount_paise), else_=0)).label("gross_spend"),
            func.sum(case((Transaction.transaction_type == "refund", Transaction.amount_paise), else_=0)).label("refund"),
            func.sum(case((Transaction.transaction_type == "income", Transaction.amount_paise), else_=0)).label("cashback"),
        )
        .where(
            Transaction.user_id == user_id,
            Transaction.transaction_type.in_(("spend", "refund", "income")),
            Transaction.date >= ytd_first,
            Transaction.date <= last,
        )
        .group_by(Transaction.account_id)
    )
    ytd_stmt = confirmed_only(ytd_stmt)
    ytd_spend: dict[int, int] = {}
    ytd_gross_spend: dict[int, int] = {}
    ytd_refund: dict[int, int] = {}
    ytd_cashback: dict[int, int] = {}
    for row in session.execute(ytd_stmt).all():
        m = row._mapping
        acct_id = m["account_id"]
        gs = int(m["gross_spend"] or 0)
        rf = int(m["refund"] or 0)
        cb = int(m["cashback"] or 0)
        ytd_spend[acct_id] = gs + rf
        ytd_gross_spend[acct_id] = gs
        ytd_refund[acct_id] = rf
        ytd_cashback[acct_id] = cb

    accounts = [
        AccountBalanceRow(
            account_id=a.id,
            name=a.name,
            type=a.type,
            currency=a.currency,
            balance_paise=a.opening_balance_paise + summed.get(a.id, 0),
            spend_ytd_paise=(ytd_spend.get(a.id, 0) if a.type == "credit_card" else None),
            gross_spend_ytd_paise=(ytd_gross_spend.get(a.id, 0) if a.type == "credit_card" else None),
            refund_ytd_paise=(ytd_refund.get(a.id, 0) if a.type == "credit_card" else None),
            cashback_ytd_paise=(ytd_cashback.get(a.id, 0) if a.type == "credit_card" else None),
            archived=a.archived_at is not None,
        )
        for a in session.scalars(
            select(Account).where(Account.user_id == user_id).order_by(Account.name)
        )
    ]
    # Which types contribute is NOT decided here — NET_WORTH_EXCLUDED_TYPES is the
    # one home for that policy (app/schemas/dashboards.py), so the un-built
    # over-time series can't derive a second answer. Today it excludes credit
    # cards (spend channels, not liabilities) and investment accounts
    # (placeholders — their value is already in portfolio_value_paise below), and
    # bank / cash contribute their signed balance, so a bank overdraft still
    # legitimately reduces net worth. The accounts list above reports each raw
    # signed balance regardless; a CC's calendar-YTD spend rides along on
    # spend_ytd_paise.
    net_worth_accounts = sum(
        a.balance_paise for a in accounts if a.type not in NET_WORTH_EXCLUDED_TYPES
    )

    # Month income / expense — the SAME helper period_totals calls, so the landing-page
    # tile and the drill-down route can no longer disagree for one ?month=.
    income_paise, expense_paise = _income_expense_sums(
        session, user_id=user_id, start=first, end=last
    )

    # Portfolio current value: skip null-NAV holdings (count as 0). USD holdings convert
    # at the newest cached rate — this is a "now" view with no as-of date, so it must not
    # depend on which timezone the host thinks "today" is (see /holdings for the same
    # read). A priced USD holding with no cached rate at all is FX-unavailable → excluded
    # from the rollup and surfaced via fx_unavailable_count, never silently dropped from
    # net worth. (/portfolio differs deliberately: it carries a real as_of anchor, so it
    # resolves the rate at that date.)
    usd_inr = latest_rate(session)
    portfolio_rollup = summarize_holdings(
        compute_holdings(session, user_id=user_id, usd_inr_rate=usd_inr)
    )
    portfolio_value_paise = portfolio_rollup.current_value_paise

    return OverviewResponse(
        month=month,
        net_worth_paise=net_worth_accounts + portfolio_value_paise,
        portfolio_value_paise=portfolio_value_paise,
        fx_unavailable_count=portfolio_rollup.fx_unavailable_count,
        income_paise=income_paise,
        expense_paise=expense_paise,
        net_paise=income_paise + expense_paise,
        accounts=accounts,
    )


@router.get("/tagging-stats", response_model=TaggingStatsResponse)
def tagging_stats(
    session: SessionDep,
    user_id: CurrentUserId,
) -> TaggingStatsResponse:
    """Auto-tag acceptance rate (PRD §F3 / §Success-metrics: ≥80% pre-tagged).

    Of board rows the import auto-tagged to a **still-live** category
    (``auto_category_id`` points at a non-archived bucket), the fraction whose
    final ``category_id`` still equals the frozen suggestion — final-state
    semantics (a change-then-change-back counts as kept). No transaction_type
    filter is needed: ``auto_category_id`` is only ever set for AUTO_TAGGABLE_TYPES
    rows by construction (import_service). ``acceptance_rate`` is ``None`` at zero
    denominator so the UI shows "no data", not "0%".

    Current-health semantics: rows whose frozen suggestion points at a
    since-archived category are excluded from BOTH numerator and denominator via
    the inner join below. This drops (a) stale "kept forever" votes for dead
    buckets and (b) the noise where an archived-suggestion row re-bucketed to
    Other at commit (F4a) would otherwise read as not-kept. The join is keyed on
    ``auto_category_id`` (the suggestion), never the final ``category_id`` — a row
    the user changed to a since-archived bucket is a genuine reject and must stay
    counted as not-kept. Deliberately asymmetric with spend-by-category (which
    keeps archived buckets): that answers "where did money go, ever"; this answers
    "is auto-tagging healthy now". See ADR-0004.
    """
    kept_expr = func.sum(
        case((Transaction.category_id == Transaction.auto_category_id, 1), else_=0)
    )
    stmt = (
        select(func.count(), kept_expr)
        .join(
            Category,
            and_(
                Category.id == Transaction.auto_category_id,
                Category.user_id == user_id,
                Category.archived_at.is_(None),
            ),
        )
        .where(
            Transaction.user_id == user_id,
            # Redundant with the inner join, but keeps intent legible.
            Transaction.auto_category_id.is_not(None),
        )
    )
    stmt = confirmed_only(stmt)
    total_raw, kept_raw = session.execute(stmt).one()
    total_auto = int(total_raw or 0)
    kept = int(kept_raw or 0)

    rules_count = session.scalar(
        select(func.count()).select_from(MerchantTagMap).where(MerchantTagMap.user_id == user_id)
    )
    return TaggingStatsResponse(
        total_auto_tagged=total_auto,
        kept=kept,
        acceptance_rate=(kept / total_auto) if total_auto else None,
        rules_count=int(rules_count or 0),
    )
