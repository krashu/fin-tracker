"""``/api/v1/dashboards`` — read-only aggregate views (PRD §F8).

v1 ships ``GET /dashboards/spend-by-category`` (PRD §F8 view 2),
``GET /dashboards/spend-by-period`` (view 3, the weekly/monthly spend bar) and
``GET /dashboards/category-trend`` (view 6, monthly spend per parent category).
The remaining views (portfolio tiles, net worth over time) depend on F7 / FX
wiring and land later.

Routes here are pure aggregation: signed-sum SQL over the existing
``transactions`` table, scoped to the current user, filtered to the
review-committed board (``confirmed_at IS NOT NULL``). No service layer —
there's no business logic, just an indexed GROUP BY, beyond the per-user alias
fold (ADR-0011) that top-merchants and tagging-stats apply in Python.
"""

from __future__ import annotations

import calendar
import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Annotated, Literal, NamedTuple
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.api.deps import CurrentUserId, SessionDep
from app.core import clock
from app.core.fiscal_year import parse_year
from app.models import Account, Category, Label, MerchantTagMap, Transaction, TransactionLabel
from app.schemas import (
    NET_WORTH_EXCLUDED_TYPES,
    AccountBalanceRow,
    AvailableYearsResponse,
    CashflowByPeriodBucket,
    CashflowByPeriodResponse,
    CategoryTrendParent,
    CategoryTrendResponse,
    CategoryTrendSubcategory,
    OverviewResponse,
    PeriodTotalsResponse,
    SpendByCategoryResponse,
    SpendByCategoryRow,
    SpendByPeriodBucket,
    SpendByPeriodResponse,
    SpendByTagByPeriodBucket,
    SpendByTagByPeriodResponse,
    SpendByTagPeriodTotal,
    SpendByTagResponse,
    SpendByTagRow,
    TaggingStatsResponse,
    TagRef,
    TopMerchantRow,
    TopMerchantsResponse,
)
from app.services.category_colors import category_colors
from app.services.category_service import CASHBACK_CATEGORY_NAME
from app.services.fx_service import latest_rate
from app.services.holdings_service import compute_holdings, summarize_holdings
from app.services.merchant_alias import is_unknown_via, load_alias_resolver
from app.services.tag_service import AUTO_TAGGABLE_TYPES
from app.services.transaction_queries import confirmed_only

router = APIRouter(prefix="/dashboards", tags=["dashboards"])

# Anchored YYYY-MM with month in [01, 12], or a bare YYYY. Validated route-side
# rather than via Query(pattern=) so FastAPI's RequestValidationError can't echo
# the rejected input — matches the imports.py:80 input-echo discipline.
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


# ---------- route prologue ----------------------------------------------------
#
# The shape every period-taking route in this file opens with, hand-copied 4 / 5
# times (A1.2/A2.2). Extracted for the reason :448 already gives for `_bucket_of`
# and `_iter_periods`: the next edit is a one-line diff here instead of a
# multi-block sweep that lands on one route slightly different from the rest —
# which is exactly how `overview` used to report a different span than
# `spend-by-category` for what the user thought was "the same period".


class PeriodWindow(NamedTuple):
    """A validated period — a calendar month (``YYYY-MM``) or a calendar year
    (``YYYY``: ``2026`` = 1 January – 31 December 2026, PRD §F8; only tax uses the
    financial year) — expanded to its inclusive calendar-date bounds.

    One shape for both request forms, replacing ``MonthWindow`` (month-only) +
    ``PeriodBounds`` (month-or-year, but re-deriving the month case through
    ``_month_window`` instead of owning it). ``mon`` is ``None`` for a year
    window, and ``year`` is the calendar year either way. ``key`` is the
    canonical wire echo — ``month`` or ``year`` as given, verbatim — the single
    ``period`` field every period-taking response now carries instead of an
    optional ``month``/``year`` pair plus a pop-if-absent ``@model_serializer``.

    The boundary is calendar-local: ``Transaction.date`` is a naive calendar
    date and ``confirmed_at`` (UTC) is **not** consulted, so a row dated
    ``2026-05-31`` lands in the May bucket regardless of its ``confirmed_at``
    instant. No month/year route defaults to "current period" — the user's timezone
    is the frontend's concern, so one of ``month`` / ``year`` is always required
    (``category-trend`` is the exception: it is anchored on ``clock.today()``).
    """

    first: date
    last: date
    year: int
    mon: int | None
    key: str


def _period_window(month: str | None, year: str | None) -> PeriodWindow:
    """Validate ``month`` xor ``year`` into a :class:`PeriodWindow`.

    422s on a malformed or missing value with a generic detail — the rejected
    input is never echoed back (input-echo discipline; see imports.py:80).
    """
    if year is not None:
        y = parse_year(year)
        if y is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="year must match YYYY",
            )
        return PeriodWindow(first=date(y, 1, 1), last=date(y, 12, 31), year=y, mon=None, key=year)
    if month is not None:
        if not _MONTH_RE.match(month):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="month must match YYYY-MM",
            )
        y, mon = int(month[:4]), int(month[5:7])
        return PeriodWindow(
            first=date(y, mon, 1),
            last=date(y, mon, calendar.monthrange(y, mon)[1]),
            year=y,
            mon=mon,
            key=month,
        )
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail="month or year is required",
    )


@router.get("/available-years", response_model=AvailableYearsResponse)
def available_years(
    session: SessionDep,
    user_id: CurrentUserId,
) -> AvailableYearsResponse:
    """Calendar years worth offering in a spend year picker, newest first (PRD §F8).

    Every year from the earliest to the latest of the user's confirmed transactions
    and the current year (of ``clock.today()``, the IST business date), with no gap —
    so a just-registered user with no data yet still sees this year as an option, and a
    future-dated row
    (a manual entry, or a backfilled import) is still reachable rather than
    clipped off. One MIN/MAX pass, not a
    separate current-year comparison computed twice against itself (which is
    a no-op ``max(x, x)`` and can silently return ``[]`` when every row
    predates or postdates the current year).
    """
    min_date, max_date = session.execute(
        confirmed_only(
            select(func.min(Transaction.date), func.max(Transaction.date)).where(
                Transaction.user_id == user_id
            )
        )
    ).one()
    current = clock.today().year
    start = min(current, min_date.year) if min_date else current
    end = max(current, max_date.year) if max_date else current
    return AvailableYearsResponse(years=list(range(end, start - 1, -1)))


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
    """Signed totals over a date range. ``expense_paise`` is Σ(spend, signed)."""

    income_paise: int
    expense_paise: int


def _income_expense_sums(
    session: Session, *, user_id: UUID, start: date, end: date
) -> IncomeExpense:
    """Signed income / expense totals over the inclusive ``[start, end]``, confirmed only.

    ``expense_paise`` is the signed Σ(spend, signed): negative on an ordinary window and
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
                Transaction.transaction_type == "spend",
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
        Transaction.transaction_type.in_(("spend", "income")),
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
    """Signed-sum spend per category for the given calendar month or calendar year.

    Each row also names its parent (``parent_id`` / ``parent_name``), joined on id +
    ``user_id`` and never on ``archived_at``, so a rollup by parent still places an
    archived subcategory under it (ADR-0012 rule 6). ``display_color`` and
    ``parent_display_color`` are the two rows' server-resolved colours, from
    ``category_colors`` whatever the archive state (ADR-0012 rule 7).
    """
    window = _period_window(month, year)
    first, last = window.first, window.last

    total_paise = func.sum(Transaction.amount_paise).label("total_paise")
    parent = aliased(Category)

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
            Category.parent_id.label("parent_id"),
            parent.name.label("parent_name"),
            total_paise,
        )
        .outerjoin(
            Category,
            and_(
                Category.id == Transaction.category_id,
                Category.user_id == user_id,
            ),
        )
        .outerjoin(
            parent,
            and_(parent.id == Category.parent_id, parent.user_id == user_id),
        )
        .where(
            Transaction.user_id == user_id,
            Transaction.transaction_type == "spend",
            Transaction.date >= first,
            Transaction.date <= last,
        )
        .group_by(Transaction.category_id, Category.name, Category.parent_id, parent.name)
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

    colors = category_colors(session, user_id=user_id)
    rows = [
        SpendByCategoryRow(
            category_id=cat_id,
            category_name=cat_name,
            parent_id=parent_id,
            parent_name=parent_name,
            display_color=colors[cat_id].display if cat_id in colors else None,
            parent_display_color=colors[parent_id].display if parent_id is not None else None,
            total_paise=int(total),
        )
        for cat_id, cat_name, parent_id, parent_name, total in session.execute(stmt).all()
    ]
    return SpendByCategoryResponse(period=window.key, rows=rows, label_id=label_id)


@router.get("/spend-by-tag", response_model=SpendByTagResponse)
def spend_by_tag(
    session: SessionDep,
    user_id: CurrentUserId,
    month: Annotated[str | None, Query()] = None,
    year: Annotated[str | None, Query()] = None,
) -> SpendByTagResponse:
    """Signed-sum spend per tag for the given calendar month or calendar year."""
    window = _period_window(month, year)
    first, last = window.first, window.last

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
            Transaction.transaction_type == "spend",
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
        Transaction.transaction_type == "spend",
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
        period=window.key,
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
    """Biggest-spender merchants for the given calendar month or calendar year, grouped by
    the per-user canonical merchant (ADR-0011 rule 8)."""
    window = _period_window(month, year)
    first, last = window.first, window.last

    # merchant_normalized != "" drops the no-merchant bucket, so total_merchants
    # counts only merchants that can actually appear in `rows`.
    where = [
        Transaction.user_id == user_id,
        Transaction.transaction_type == "spend",
        Transaction.date >= first,
        Transaction.date <= last,
        Transaction.merchant_normalized != "",
    ]
    if label_id is not None:
        # EXISTS subquery (see spend_by_category / transactions.py) — no join-row
        # duplication of the grouped sum.
        where.append(Transaction.labels.any(Label.id == label_id))

    stmt = confirmed_only(
        select(
            Transaction.merchant_normalized,
            func.max(Transaction.merchant_raw),
            func.sum(Transaction.amount_paise),
        )
        .where(*where)
        .group_by(Transaction.merchant_normalized)
    )
    # Aliases fold several normalized keys into one canonical, so the grouping is finished
    # here: SQL sums per normalized key, Python re-sums per canonical.
    resolver = load_alias_resolver(session, user_id=user_id)
    totals: dict[str, int] = {}
    labels: dict[str, str] = {}
    for merchant_normalized, raw_max, total in session.execute(stmt):
        canonical = resolver.canonical(merchant_normalized)
        totals[canonical] = totals.get(canonical, 0) + int(total)
        # An identity group keeps a representative raw descriptor; an aliased or
        # unknown-via group shows its canonical.
        if merchant_normalized == canonical and raw_max:
            labels[canonical] = raw_max
    # Most-negative (biggest spend) first; canonical asc is a stable tiebreak for equal totals.
    ranked = sorted(totals.items(), key=lambda kv: (kv[1], kv[0]))
    rows = [
        TopMerchantRow(
            merchant_canonical=canonical,
            merchant_label=labels.get(canonical, canonical),
            total_paise=total,
        )
        for canonical, total in ranked[:limit]
    ]
    total_merchants = len(totals)

    return TopMerchantsResponse(
        period=window.key,
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

    Type filter ``transaction_type == "spend"`` (refunds are the positive rows),
    board-only (``confirmed_at``), and the
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
        Transaction.transaction_type == "spend",
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

    Type filter ``("spend", "income")`` and board-only
    (``confirmed_at``) match ``period_totals``: ``income_paise`` = **signed** Σ income
    (an income reversal nets it down, ADR-0009 rule 5);
    ``expense_paise`` = **signed** Σ(spend, signed) (≤ 0 in the common case but
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
        Transaction.transaction_type.in_(("spend", "income")),
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
        elif ttype == "spend":
            # Signed: refunds are the positive spend rows, so they net here.
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
    the tag analog of ``category_trend``. It combines two shapes that
    already exist: the group-by-tag JOIN from ``spend_by_tag`` (arc decision #7 —
    the INNER JOIN over ``transaction_labels`` + ``Label``, which INTENTIONALLY
    double-counts a multi-tagged txn across its tags) and the clipped-window
    Python-side ``_bucket_of`` / ``_iter_periods`` bucketing + zero-fill from
    ``spend_by_period`` / ``category_trend``. Window / type filter
    (``transaction_type == "spend"``) / board-only (``confirmed_at``) / INR-only signed-int
    convention are all identical to those routes (``income`` and ``transfer``
    excluded).

    Two things differ from ``category_trend``, both flowing from tags
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
            Transaction.transaction_type == "spend",
            Transaction.date >= start,
            Transaction.date <= end,
        )
    )
    # Board rows only (confirmed_at IS NOT NULL) — shared with the sibling routes.
    stmt = confirmed_only(stmt)

    # Flat accumulators (int money type; mirrors category_trend's flat
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

    Board-only signed sums in one pass: ``expense`` = Σ(spend, signed) (signed,
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
    month: Annotated[str | None, Query()] = None,
    year: Annotated[str | None, Query()] = None,
) -> OverviewResponse:
    """Financial Overview home aggregate (PRD §F8 view 1 + view 4).

    One call backs the /dashboard landing: per-account current balances, net
    worth, current portfolio value, and the requested period's income / expense
    / net. Takes the same month-or-year period every other dashboard route
    does (:func:`_period_window`) — previously this was the one route stuck on
    a required ``month``, which is what pushed the frontend to fake a year by
    sending ``YYYY-12``: ``income_paise`` came back December-only while a
    sibling call's ``spend_ytd_paise`` for the same "year" was the full year.

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
    ``spend_ytd_paise`` (calendar-year-to-date spend). Investment accounts are
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
    ``period-totals`` calls, over this period's window — one implementation, so the
    two endpoints cannot report different totals for the same period. ``expense`` is
    the signed Σ(spend, signed) ≤ 0, ``net`` = income + expense. ``transfer`` is
    excluded everywhere.
    """
    window = _period_window(month, year)
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

    # Per-CC calendar-year-to-date spend (PRD §F8: spend years run 1 January –
    # 31 December): signed net Σ(spend, signed) over [1 January of the year the
    # requested period starts in, end of the requested period]. Window derives strictly from
    # `month`/`year` (`window.first` / `window.last`, both parsed by _period_window),
    # never the clock — the route stays deterministic on its input. A year request's
    # "YTD" is the whole year (window.first is already 1 January, window.last 31 December),
    # so this generalizes without a special case.
    # Signed and never clamped here (a refund-dominant window is legitimately
    # positive; the frontend floors to a non-negative "spent" magnitude). Only
    # credit-card accounts get a value; every other type maps to None below.
    # Only spend rows count, split three ways (ADR-0013 rule 4): gross_spend is the
    # negative ones; a positive one (a refund, ADR-0009, or a purchase cashback,
    # ADR-0013) is `cashback` if its category is the spend "Cashback" or a child
    # of it (one hop, ADR-0012 rule 6; archive state ignored), else `refund`. The
    # three are exhaustive because zero-paise amounts are rejected at every write
    # path, so gross_spend + refund + cashback == the signed net. The refund case
    # orders its whens rather than negating is_cashback: for an uncategorized row
    # that predicate is NULL, and NOT NULL would silently drop the refund.
    # Inlined rather than extracted: this is its only backend consumer.
    ytd_first = date(window.first.year, 1, 1)
    ytd_cat = aliased(Category)
    ytd_parent = aliased(Category)
    is_spend = Transaction.transaction_type == "spend"
    is_cashback = or_(
        and_(ytd_cat.kind == "spend", ytd_cat.name == CASHBACK_CATEGORY_NAME),
        and_(ytd_parent.kind == "spend", ytd_parent.name == CASHBACK_CATEGORY_NAME),
    )
    gross_spend_sum = func.sum(
        case((and_(is_spend, Transaction.amount_paise < 0), Transaction.amount_paise), else_=0)
    ).label("gross_spend")
    refund_sum = func.sum(
        case(
            (and_(is_spend, Transaction.amount_paise > 0, is_cashback), 0),
            (and_(is_spend, Transaction.amount_paise > 0), Transaction.amount_paise),
            else_=0,
        )
    ).label("refund")
    cashback_sum = func.sum(
        case(
            (and_(is_spend, Transaction.amount_paise > 0, is_cashback), Transaction.amount_paise),
            else_=0,
        )
    ).label("cashback")
    ytd_stmt = (
        select(Transaction.account_id, gross_spend_sum, refund_sum, cashback_sum)
        .outerjoin(
            ytd_cat,
            and_(ytd_cat.id == Transaction.category_id, ytd_cat.user_id == user_id),
        )
        .outerjoin(
            ytd_parent,
            and_(ytd_parent.id == ytd_cat.parent_id, ytd_parent.user_id == user_id),
        )
        .where(
            Transaction.user_id == user_id,
            is_spend,
            Transaction.date >= ytd_first,
            Transaction.date <= last,
        )
        .group_by(Transaction.account_id)
    )
    ytd_stmt = confirmed_only(ytd_stmt)
    # Three accumulators, not four: `spend_ytd_paise` below is the signed net
    # gross_spend + refund + cashback, derived inline at the point of use rather
    # than carried as its own dict that has to be kept in lockstep with the other
    # three on every write.
    ytd_gross_spend: dict[int, int] = {}
    ytd_refund: dict[int, int] = {}
    ytd_cashback: dict[int, int] = {}
    for row in session.execute(ytd_stmt).all():
        m = row._mapping
        acct_id = m["account_id"]
        ytd_gross_spend[acct_id] = int(m["gross_spend"] or 0)
        ytd_refund[acct_id] = int(m["refund"] or 0)
        ytd_cashback[acct_id] = int(m["cashback"] or 0)

    accounts = [
        AccountBalanceRow(
            account_id=a.id,
            name=a.name,
            type=a.type,
            currency=a.currency,
            balance_paise=a.opening_balance_paise + summed.get(a.id, 0),
            spend_ytd_paise=(
                ytd_gross_spend.get(a.id, 0) + ytd_refund.get(a.id, 0) + ytd_cashback.get(a.id, 0)
                if a.type == "credit_card"
                else None
            ),
            gross_spend_ytd_paise=(
                ytd_gross_spend.get(a.id, 0) if a.type == "credit_card" else None
            ),
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

    # Period income / expense — the SAME helper period_totals calls, so the landing-page
    # tile and the drill-down route can no longer disagree for one period.
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
        period=window.key,
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
    """Two distinct F3 health metrics (PRD §F3 / §Success-metrics), neither a
    replacement for the other: ``acceptance_rate`` (of what we suggested, did
    it stick?) and ``coverage_rate`` (of what we imported, did we suggest
    anything at all? — the ≥80% pre-tag bar).

    ``acceptance_rate``: of board rows the import auto-tagged to a
    **still-live** category (``auto_category_id`` points at a non-archived
    bucket), the fraction whose final ``category_id`` still equals the frozen
    suggestion — final-state semantics (a change-then-change-back counts as
    kept). No transaction_type filter is needed: ``auto_category_id`` is only
    ever set for AUTO_TAGGABLE_TYPES rows by construction (import_service).
    ``acceptance_rate`` is ``None`` at zero denominator so the UI shows "no
    data", not "0%".

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

    ``coverage_rate``: ``imported_total`` is every AUTO-TAGGABLE board row
    imported (``source == "import"`` and ``transaction_type`` in
    ``AUTO_TAGGABLE_TYPES``), regardless of whether it was auto-tagged;
    ``pre_tagged`` is the exact same "auto-tagged to a still-live category"
    population as ``total_auto`` above — acceptance's denominator and
    coverage's numerator are identical by construction, just answering
    different questions. ``coverage_rate`` is ``None`` at zero
    ``imported_total``, same "no data" contract as ``acceptance_rate``.

    The type filter is load-bearing here, unlike on ``acceptance_rate`` above:
    ``import_service`` sets ``auto_category_id`` only for ``AUTO_TAGGABLE_TYPES``,
    so income and transfer rows can never enter the numerator. Counting them in
    the denominator made the metric structurally unable to reach 100% and
    under-report by each statement's income/transfer share — 40 of 40 spend rows
    pre-tagged alongside 10 bill-payment credits read 0.80, exactly the PRD
    §Success-metrics bar it was added to measure.
    """
    # coalesce: a link moves a row's category into pre_link_category_id (ADR-0002 rule 9), so
    # the user's current choice is whichever of the two is set. The CHECK guarantees they are
    # never both set, which makes the coalesce exact.
    kept_expr = func.sum(
        case(
            (
                func.coalesce(Transaction.category_id, Transaction.pre_link_category_id)
                == Transaction.auto_category_id,
                1,
            ),
            else_=0,
        )
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

    # pre_tagged is the exact same "auto-tagged to a still-live category"
    # population as total_auto — acceptance_rate's denominator and
    # coverage_rate's numerator are identical by construction (auto_category_id
    # is only ever set by import_service). Exposed separately because each
    # answers a different question: of what we suggested, did it stick? vs of
    # what we imported, did we suggest anything at all?
    pre_tagged = total_auto

    imported_total = int(
        session.scalar(
            confirmed_only(
                select(func.count()).where(
                    Transaction.user_id == user_id,
                    Transaction.source == "import",
                    # Sorted for a stable, cacheable IN list.
                    Transaction.transaction_type.in_(sorted(AUTO_TAGGABLE_TYPES)),
                )
            )
        )
        or 0
    )

    # Live, non-seeded rules (ADR-0004 rule 10, ADR-0011 rule 11): the category
    # must be live, and the row not a never-confirmed seed (``hit_count == 0`` and
    # not pinned — a pin is user-authored, so it counts). Seed rows are excluded so
    # a fresh user's dictionary doesn't inflate "learned rules" before any import.
    # A key that resolves to an unknown-via canonical (a gateway handle) is not a rule
    # either (ADR-0011 rule 12), which only the resolver can tell, so the keys come back
    # and are counted in Python.
    rule_keys = session.scalars(
        select(MerchantTagMap.merchant_normalized)
        .join(
            Category,
            and_(
                Category.id == MerchantTagMap.category_id,
                Category.user_id == user_id,
                Category.archived_at.is_(None),
            ),
        )
        .where(
            MerchantTagMap.user_id == user_id,
            or_(MerchantTagMap.hit_count > 0, MerchantTagMap.pinned.is_(True)),
        )
    )
    resolver = load_alias_resolver(session, user_id=user_id)
    return TaggingStatsResponse(
        total_auto_tagged=total_auto,
        kept=kept,
        acceptance_rate=(kept / total_auto) if total_auto else None,
        rules_count=sum(1 for key in rule_keys if not is_unknown_via(resolver.canonical(key))),
        imported_total=imported_total,
        pre_tagged=pre_tagged,
        coverage_rate=(pre_tagged / imported_total) if imported_total else None,
    )


def _trailing_month_window(today: date, months: int) -> tuple[date, date]:
    """Whole calendar months ending in ``today``'s month, current month included.

    Returns the first day of the oldest month and the last day of ``today``'s
    month, so a future-dated row later this month still counts.
    """
    idx = today.year * 12 + today.month - 1 - (months - 1)
    first = date(idx // 12, idx % 12 + 1, 1)
    last = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    return first, last


@dataclass(slots=True)
class _TrendAcc:
    name: str | None
    archived: bool
    totals: list[int]
    is_direct: bool = False
    subs: dict[int, _TrendAcc] = field(default_factory=dict)


@router.get("/category-trend", response_model=CategoryTrendResponse)
def category_trend(
    session: SessionDep,
    user_id: CurrentUserId,
    months: Annotated[int, Query(ge=1, le=60)] = 12,
    label_id: Annotated[int | None, Query(gt=0)] = None,
) -> CategoryTrendResponse:
    """Monthly signed spend per parent category with a subcategory drilldown
    (PRD §F8 view 6), over the trailing ``months`` whole calendar months ending
    in the IST business month (anchored on ``clock.today()``, not a picker).

    Values are signed (ADR-0009 / ADR-0013) and never clamped. A subcategory,
    archived or not, rolls into its parent (ADR-0012 rule 6), so each parent's
    series is the sum of its ``subcategories``; spend booked straight to a
    parent is its ``is_direct`` row. Every parent with an in-window row is
    returned and the frontend folds the tail. For each month the sum over
    parents equals that month's ``spend-by-period`` total. Uncategorized spend
    is the ``parent_id=None`` entry, last, with no subcategories.

    Colours come from ``category_colors`` (ADR-0012 rule 7): the parent's ``display_color`` on
    each parent, and each series' rotated ``series`` colour on its subcategory.
    """
    first, last = _trailing_month_window(clock.today(), months)
    periods = list(_iter_periods(first, last, "month"))
    slot = {p: i for i, p in enumerate(periods)}

    Parent = aliased(Category)
    stmt = (
        select(
            Transaction.date,
            Transaction.category_id,
            Transaction.amount_paise,
            Category.name.label("category_name"),
            Category.parent_id,
            Category.archived_at.label("category_archived_at"),
            Parent.name.label("parent_name"),
            Parent.archived_at.label("parent_archived_at"),
        )
        .outerjoin(
            Category,
            and_(Category.id == Transaction.category_id, Category.user_id == user_id),
        )
        .outerjoin(
            Parent,
            and_(Parent.id == Category.parent_id, Parent.user_id == user_id),
        )
        .where(
            Transaction.user_id == user_id,
            Transaction.transaction_type == "spend",
            Transaction.date >= first,
            Transaction.date <= last,
        )
    )
    stmt = confirmed_only(stmt)
    if label_id is not None:
        stmt = stmt.where(Transaction.labels.any(Label.id == label_id))

    n = len(periods)
    accs: dict[int | None, _TrendAcc] = {}
    for row in session.execute(stmt).all():
        i = slot[_bucket_of(row.date, "month").period]
        amt = int(row.amount_paise)
        cat_id = row.category_id
        if cat_id is None:
            parent = accs.setdefault(None, _TrendAcc(None, False, [0] * n))
            parent.totals[i] += amt
            continue
        if row.parent_id is None:
            pkey = cat_id
            meta = (row.category_name, row.category_archived_at is not None)
            is_direct = True
        else:
            pkey = row.parent_id
            meta = (row.parent_name, row.parent_archived_at is not None)
            is_direct = False
        parent = accs.setdefault(pkey, _TrendAcc(*meta, [0] * n))
        parent.totals[i] += amt
        sub = parent.subs.get(cat_id)
        if sub is None:
            sub = parent.subs[cat_id] = _TrendAcc(
                row.category_name,
                row.category_archived_at is not None,
                [0] * n,
                is_direct=is_direct,
            )
        sub.totals[i] += amt

    colors = category_colors(session, user_id=user_id)
    ordered = sorted(accs.items(), key=lambda kv: (kv[0] is None, sum(kv[1].totals), kv[0] or 0))
    parents = [
        CategoryTrendParent(
            parent_id=pid,
            parent_name=acc.name,
            color=colors[pid].display if pid is not None else None,
            archived=acc.archived,
            totals_paise=acc.totals,
            subcategories=[
                CategoryTrendSubcategory(
                    category_id=cid,
                    category_name=s.name,
                    color=colors[cid].series,
                    archived=s.archived,
                    is_direct=s.is_direct,
                    totals_paise=s.totals,
                )
                for cid, s in sorted(
                    acc.subs.items(),
                    key=lambda kv: (kv[1].is_direct, sum(kv[1].totals), kv[0]),
                )
            ],
        )
        for pid, acc in ordered
    ]
    return CategoryTrendResponse(periods=periods, parents=parents, label_id=label_id)
