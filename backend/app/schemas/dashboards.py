"""Dashboard response schemas (PRD §F8).

v1 ships one view in this module: **monthly spend by category** (PRD §F8
view 2). View 1 (portfolio tiles), view 3 (weekly/monthly bar), and view 4
(net worth over time) are deferred until F7 (investments) lands.

``SpendByCategoryResponse`` is a ``{period, rows}`` envelope rather than a
flat list: the echoed ``period`` lets the frontend verify it got the period
it asked for under stale-cache races. ``transaction_count`` is deliberately
omitted from rows — PRD §F8 view 2 (bar/pie + drilldown) doesn't need it,
and adding it now would be scope creep (CLAUDE.md §2).
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel

from app.models.account import AccountTypeStr, CurrencyStr


class SpendByCategoryRow(BaseModel):
    """One row of the spend-by-category aggregate.

    ``total_paise`` is signed: spends are negative, refunds are positive, so
    a category whose refunds outweigh in-window spend surfaces with a
    positive total (PRD §F4a rule 3: signed sums net refunds against
    category spend).

    ``category_id`` / ``category_name`` are both null for the uncategorized
    bucket — rows where ``transactions.category_id IS NULL``.
    """

    category_id: int | None
    category_name: str | None
    total_paise: int


class AvailableYearsResponse(BaseModel):
    """Envelope for GET /api/v1/dashboards/available-years."""

    years: list[int]


class SpendByCategoryResponse(BaseModel):
    """Envelope for ``GET /api/v1/dashboards/spend-by-category``.

    ``period`` echoes the requested ``month`` (``"YYYY-MM"``) or ``year``
    (``"YYYY"``) verbatim — a single always-present field (``PeriodWindow.key``)
    rather than an optional ``month``/``year`` pair, since exactly one of the
    two was ever requested. ``rows`` is ordered most-negative first (= biggest
    spend first), with the uncategorized row (``category_id is None``) pinned
    last regardless of magnitude — see the route for the portable sort.

    ``label_id`` echoes the optional F3a tag filter (``None`` = unfiltered), so
    the client can confirm which tag the rows are scoped to under stale-cache
    races — same rationale as ``period``.
    """

    period: str
    rows: list[SpendByCategoryRow]
    label_id: int | None = None


class SpendByTagRow(BaseModel):
    """One row of the spend-by-tag breakdown (PRD §F3a labels; tag-analysis arc
    Phase B).

    ``total_paise`` is signed exactly like ``SpendByCategoryRow`` (spends
    negative, refunds positive — refunds net against spend *within* the tag), so
    a tag whose refunds outweigh its in-window spend surfaces positive.

    ``label_id`` / ``label_name`` are both ``None`` for the **untagged bucket** —
    the signed sum of txns carrying zero labels, pinned last (mirrors
    ``SpendByCategoryRow``'s uncategorized bucket, arc decision #4).
    """

    label_id: int | None
    label_name: str | None
    total_paise: int


class SpendByTagResponse(BaseModel):
    """Envelope for ``GET /api/v1/dashboards/spend-by-tag`` — the /spending
    spend-by-tag breakdown + its coverage guardrail (tag-analysis arc Phase B).

    ``period`` echoes the requested month or year (stale-cache verification, cf.
    ``SpendByCategoryResponse``).

    ``rows`` are the per-tag signed sums (most-negative first, ``label_id``
    ascending as a stable tiebreak) with the untagged-bucket row
    (``label_id is None``) pinned last. **The per-tag rows legitimately
    DOUBLE-COUNT multi-tagged transactions** — a txn tagged ``#travel`` +
    ``#work`` contributes its full amount to *both* rows — because tags are
    many:many (arc decision #7, the group-by shape). Consequently
    ``Σ(rows.total_paise)`` deliberately overshoots ``total_spend_paise``; this
    is correct and must never be "fixed" by clamping or de-duping.

    Coverage (arc decisions #2 / #5 — the honesty guardrail; measured by amount,
    not count):

    * ``total_spend_paise`` — the **honest** signed Σ(spend) over the
      month, computed WITHOUT the tag join, so it does not double-count. This is
      the coverage denominator, not ``Σ(rows)``.
    * ``tagged_paise`` — signed Σ over txns carrying ≥ 1 label
      (``total_spend_paise − untagged``), counting each tagged txn **once**
      regardless of how many tags it has (the EXISTS shape, not the group-by).
      This is the coverage numerator.
    * ``coverage_rate`` — ``tagged_paise / total_spend_paise`` when that lands
      cleanly in ``[0.0, 1.0]``; otherwise ``None``. It is ``None`` for the
      zero-spend month (no denominator) and for the refund-skewed month where the
      signed ratio escapes ``[0, 1]`` (e.g. untagged rows net to a credit while
      the month is net-spend → ratio > 1). ``None`` renders as "—" + the raw
      "₹A of ₹B tagged" amounts, mirroring ``TaggingStatsResponse.acceptance_rate``'s
      null-at-degenerate idiom. ``total_spend_paise`` / ``tagged_paise`` stay
      signed and are **never clamped** (arc decision #6).
    """

    period: str
    rows: list[SpendByTagRow]
    total_spend_paise: int
    tagged_paise: int
    coverage_rate: float | None


class SpendByPeriodBucket(BaseModel):
    """One bar of the spend-by-period series (PRD §F8 view 3, the spend bar).

    ``period`` is the bucket label — ``"YYYY-MM"`` for a calendar month,
    ``"YYYY-Www"`` (ISO-8601 week-date, ISO year) for a week. It is both the
    x-axis label and the chronological sort key (lexical order == time order
    for both formats).

    ``total_paise`` is signed (spend negative, refund positive — same
    nets-refunds rule as ``SpendByCategoryRow``); ``0`` for a zero-filled
    period with no in-window spend.

    Fields are intentionally limited to ``{period, total_paise}``: a spend bar
    needs a label and a height, nothing more. Per-bucket start/end dates are
    deliberately omitted (CLAUDE.md §2 — no field without a reader; the
    frontend derives any date from ``period``), exactly as
    ``SpendByCategoryRow`` omits ``transaction_count``.
    """

    period: str
    total_paise: int


class SpendByPeriodResponse(BaseModel):
    """Envelope for ``GET /api/v1/dashboards/spend-by-period``.

    ``bucket`` echoes the requested grain; ``start`` / ``end`` echo the
    requested window so the frontend can verify it got the range it asked for
    under stale-cache races (same rationale as
    ``SpendByCategoryResponse.month``). ``buckets`` is chronological ascending
    and never empty (a valid ``start <= end`` always yields at least the
    bucket containing ``start``).

    The field is named ``buckets`` (not ``rows``, cf.
    ``SpendByCategoryResponse``) on purpose — a time bucket reads better than
    "row" and matches the ``bucket=`` query param / internal ``_bucket_of``
    vocabulary. Don't "align" it to ``rows`` for false symmetry.

    ``label_id`` echoes the optional F3a tag filter (``None`` = unfiltered),
    same stale-cache-verification rationale as ``bucket`` / ``start`` / ``end``.
    """

    bucket: Literal["week", "month"]
    start: date
    end: date
    buckets: list[SpendByPeriodBucket]
    label_id: int | None = None


class CashflowByPeriodBucket(BaseModel):
    """One bar-group of the cashflow series — income vs spend for a bucket
    (PRD §F8 view 3, the /spending "am I solvent" chart).

    ``period`` is the bucket label (``"YYYY-MM"`` month | ``"YYYY-Www"`` ISO
    week), both the x-axis label and the chronological sort key — same shape as
    ``SpendByPeriodBucket.period``.

    * ``income_paise`` — Σ of ``income`` (≥ 0).
    * ``expense_paise`` — **signed** Σ of ``spend`` + ``refund``, exactly like
      ``PeriodTotalsResponse.expense_paise`` / ``SpendByPeriodBucket.total_paise``:
      ≤ 0 in the common case, but **legitimately > 0** in a refund-dominant
      bucket. The server does **not** clamp it — clamping would break the
      ``net = income + expense`` identity. Any "floor to 0" is a display-only
      decision in the frontend spend bar, never in this payload.
    * ``net_paise`` — ``income_paise + expense_paise`` (server-computed so the
      client can't drift on the sign; goes negative on a deficit bucket, which is
      the whole point of the chart). ``0`` on both income and expense for a
      zero-filled bucket with no in-window activity.

    ``transfer`` is excluded entirely (intra-account movement — neither income
    nor spend, consistent with every sibling dashboard route).
    """

    period: str
    income_paise: int
    expense_paise: int
    net_paise: int


class CashflowByPeriodResponse(BaseModel):
    """Envelope for ``GET /api/v1/dashboards/cashflow-by-period``.

    ``bucket`` echoes the requested grain; ``start`` / ``end`` echo the requested
    window (stale-cache verification, cf. ``SpendByPeriodResponse``). ``buckets``
    is chronological ascending, zero-filled, and never empty (a valid
    ``start <= end`` always yields at least the bucket containing ``start``).
    """

    bucket: Literal["week", "month"]
    start: date
    end: date
    buckets: list[CashflowByPeriodBucket]


class SpendCategoryRef(BaseModel):
    """One category in the spend-by-category-by-period **echoed set** — its id +
    name, defining the stable stack order and the color-join key for the
    /spending category-trend bar (PRD §F8 view 3).

    ``category_id`` / ``category_name`` are both ``None`` for the uncategorized
    bucket (rows where ``transactions.category_id IS NULL``). ``category_name``
    is *additionally* ``None`` for a row whose ``category_id`` points at another
    user's Category — the LEFT JOIN's ``Category.user_id == user_id`` predicate
    drops the foreign name (cf. ``SpendByCategoryRow``), an unreachable-via-API
    defense-in-depth case.
    """

    category_id: int | None
    category_name: str | None


class SpendCategoryPeriodTotal(BaseModel):
    """One cell of the category×period grid — a category's signed total in a
    bucket.

    ``total_paise`` is **signed** (spend negative, refund positive; nets refunds
    against spend exactly like ``SpendByCategoryRow.total_paise``). It is **never
    clamped** server-side: a category whose refunds outweigh its in-bucket spend
    surfaces positive. Nor is it clamped client-side — the sole consumer
    (``category-trend-bar.tsx``) is a one-category-at-a-time **signed** bar chart, so a
    net-credit month dips below the y=0 reference line the chart deliberately draws.
    There is no stacked bar here and nothing floors. Do **not** clamp server-side "for
    consistency": that would break the reconciliation identity below and read as ₹0 for
    a refund-dominant month. ``Σ`` of all cells in a bucket reconciles exactly to that
    bucket's ``SpendByPeriodBucket.total_paise``.
    """

    category_id: int | None
    total_paise: int


class SpendByCategoryByPeriodBucket(BaseModel):
    """One bucket of the category-trend series — a bucket's per-category totals.

    ``period`` is the bucket label (``"YYYY-MM"`` month | ``"YYYY-Www"`` ISO
    week), both the x-axis label and the chronological sort key — same shape as
    ``SpendByPeriodBucket.period``.

    ``totals`` is **dense**: it carries a cell for **every** category in the
    response's ``categories`` set, zero-filled — no gaps in either the period or
    the category dimension (same no-gaps contract as the period zero-fill). A
    fully zero-filled bucket (no in-window activity) still lists a 0 cell per
    category. Cell order follows ``categories`` order.
    """

    period: str
    totals: list[SpendCategoryPeriodTotal]


class SpendByCategoryByPeriodResponse(BaseModel):
    """Envelope for ``GET /api/v1/dashboards/spend-by-category-by-period`` — the
    /spending category-trend bar (PRD §F8 view 3: "how is my category mix
    shifting?"). The consumer renders ONE category at a time, chosen from a
    dropdown; the single fetch carries every category's series so switching is
    instant. It is not a stack — see ``SpendCategoryPeriodTotal`` below.

    ``bucket`` echoes the requested grain; ``start`` / ``end`` echo the requested
    window (stale-cache verification, cf. ``SpendByPeriodResponse``).

    ``categories`` is the category set appearing anywhere in the window, echoed
    once so the frontend has a stable series order + color join. Order:
    categorized first, most-negative **grand** total (Σ across all buckets)
    first → biggest overall spender at the top of the dropdown,
    ``category_id`` ascending as a stable tiebreak, uncategorized
    (``category_id is None``) pinned last — mirrors ``spend_by_category``'s
    portable sort.

    ``buckets`` is chronological ascending, zero-filled, and never empty. Each
    bucket's ``totals`` is signed and dense (one cell per ``categories`` entry).

    ``label_id`` echoes the optional F3a tag filter (``None`` = unfiltered),
    same stale-cache-verification rationale as ``bucket`` / ``start`` / ``end``.

    **Reconciliation:** ``Σ(bucket.totals[*].total_paise)`` equals the matching
    ``SpendByPeriodBucket.total_paise`` for the same window/grain (both filter
    ``spend`` + ``refund``, both signed). The identity holds on screen too: the
    consumer plots the signed magnitude against a ``y=0`` reference line, so a
    net-credit bucket dips below the axis rather than being floored — nothing is
    clamped on either side of the wire.
    """

    bucket: Literal["week", "month"]
    start: date
    end: date
    categories: list[SpendCategoryRef]
    buckets: list[SpendByCategoryByPeriodBucket]
    label_id: int | None = None


class TagRef(BaseModel):
    """One tag in the spend-by-tag-by-period **echoed set** — its id + name,
    defining the stable line order and the color-join key for the /spending
    tag-trend chart (PRD §F3a labels; tag-analysis arc Phase C).

    Unlike ``SpendCategoryRef``, ``label_id`` / ``label_name`` are **non-null**:
    the route uses an INNER JOIN over ``transaction_labels`` with
    ``Label.user_id == user_id`` (mirroring ``spend_by_tag``), so an orphan /
    foreign-user label drops the row entirely rather than surfacing a null name,
    and the untagged residual is excluded from the trend altogether (arc decision:
    the trend is about tags; coverage lives on the Phase-B card). So every tag
    here has a real id and name.
    """

    label_id: int
    label_name: str


class SpendByTagPeriodTotal(BaseModel):
    """One cell of the tag×period grid — a tag's signed total in a bucket.

    ``total_paise`` is **signed** (spend negative, refund positive; nets refunds
    against spend *within* the tag, exactly like ``SpendByTagRow.total_paise``)
    and **never clamped** server-side — a tag whose refunds outweigh its in-bucket
    spend surfaces positive (the frontend's line dips below the ``y=0`` reference).
    """

    label_id: int
    total_paise: int


class SpendByTagByPeriodBucket(BaseModel):
    """One period of the tag-trend chart — a bucket's per-tag totals.

    ``period`` is the bucket label (``"YYYY-MM"`` month | ``"YYYY-Www"`` ISO
    week), both the x-axis label and the chronological sort key — same shape as
    ``SpendByPeriodBucket.period``.

    ``totals`` is **dense**: a cell for **every** tag in the response's ``tags``
    set, zero-filled — no gaps in either the period or the tag dimension. A fully
    zero-filled bucket (no in-window tagged activity) still lists a 0 cell per tag.
    Cell order follows ``tags`` order.
    """

    period: str
    totals: list[SpendByTagPeriodTotal]


class SpendByTagByPeriodResponse(BaseModel):
    """Envelope for ``GET /api/v1/dashboards/spend-by-tag-by-period`` — the
    /spending tag-trend multi-line chart (PRD §F3a labels; tag-analysis arc
    Phase C: "is spend under a tag growing over time?").

    The tag×time generalization of ``spend_by_tag``, and the tag analog of
    ``spend_by_category_by_period``. ``bucket`` echoes the requested grain;
    ``start`` / ``end`` echo the requested window (stale-cache verification, cf.
    ``SpendByPeriodResponse``).

    ``tags`` is the tag set with any in-window activity, echoed once so the
    frontend has a stable line order + palette-by-rank color join. Order = most-
    negative **grand** total (Σ across all buckets) first → biggest overall
    spender first, ``label_id`` ascending as a stable tiebreak (mirrors
    ``spend_by_tag``'s sort). The **untagged residual is excluded** entirely (arc
    decision — the trend is about tags), so there is no null-id bucket here.

    ``buckets`` is chronological ascending, zero-filled, and never empty for a
    valid ``start <= end``. Each bucket's ``totals`` is signed and dense (one cell
    per ``tags`` entry).

    **Many:many, so NO cross-tag reconciliation identity.** The per-tag cells
    come from the group-by-tag shape (arc decision #7), so a multi-tagged txn
    **double-counts** across its tags' lines. Combined with the untagged
    exclusion, ``Σ`` of a bucket's cells does **not** equal that bucket's
    ``SpendByPeriodBucket.total_paise`` — unlike ``spend_by_category_by_period``,
    which has that identity. The only valid reconciliation is per-tag: a tag's
    cells over the window sum to that tag's ``spend_by_tag`` grouped total for the
    same window. Never "fix" the non-identity by clamping or de-duping.
    """

    bucket: Literal["week", "month"]
    start: date
    end: date
    tags: list[TagRef]
    buckets: list[SpendByTagByPeriodBucket]


class TopMerchantRow(BaseModel):
    """One row of the top-merchants list — a merchant's signed spend for a month
    (PRD §F8 view 3, "where is the money actually going?").

    ``merchant_normalized`` is the GROUP BY key (``Transaction.merchant_normalized``
    — the normalized string every raw variant collapses to). ``merchant_label`` is a
    representative display string (``COALESCE(MAX(merchant_raw), merchant_normalized)``
    — deterministic within a SQL dialect; a lexicographic pick, not a "prettiest
    name" one).

    ``total_paise`` is **signed**: spends are negative, refunds positive, so a
    merchant whose refunds outweigh in-window spend surfaces with a positive total
    (nets refunds against spend exactly like ``SpendByCategoryRow``). Rows arrive
    most-negative first (biggest spender first) — the frontend renders net-credit
    (positive) rows apart, never as a spend bar.
    """

    merchant_normalized: str
    merchant_label: str
    total_paise: int


class TopMerchantsResponse(BaseModel):
    """Envelope for ``GET /api/v1/dashboards/top-merchants`` — the /spending
    top-merchants card (PRD §F8 view 3).

    ``period`` echoes the requested month or year (stale-cache verification, cf.
    ``SpendByCategoryResponse``). ``rows`` are ordered most-negative first (biggest
    spender first, ``merchant_normalized`` ascending as a stable tiebreak) and capped
    at the requested ``limit``; the no-merchant bucket (``merchant_normalized == ""``)
    is excluded so a blank manual row can't top the list.

    ``total_merchants`` is the count of distinct non-empty merchants with in-window
    spend activity (**before** the LIMIT), so the UI can honestly say "top 5
    of 23"; ``truncated`` is ``total_merchants > limit``. ``total_merchants`` counts
    every such merchant regardless of sign, so it can exceed the number of *spend*
    rows the frontend renders as bars in the rare all-/mostly-refund month.

    ``label_id`` echoes the optional F3a tag filter (``None`` = unfiltered); when
    set, both ``rows`` and ``total_merchants`` are scoped to tagged txns, so the
    "top N of M" caption stays honest under the filter.
    """

    period: str
    rows: list[TopMerchantRow]
    total_merchants: int
    truncated: bool
    label_id: int | None = None


class PeriodTotalsResponse(BaseModel):
    """Envelope for ``GET /api/v1/dashboards/period-totals`` — income vs spend
    over ``[start, end]`` (surfaced on the /expenses summary strip).

    ``start`` / ``end`` echo the requested window (stale-cache verification,
    cf. ``SpendByPeriodResponse``). All three figures are board-only
    (``confirmed_at IS NOT NULL``) signed sums:

    * ``expense_paise`` — Σ of ``spend`` + ``refund`` (signed, so ≤ 0 in the
      common case; refunds net against spend, same rule as the spend
      aggregates). NOT a positive magnitude — the frontend negates for display,
      matching ``SpendByPeriodBucket.total_paise``.
    * ``income_paise`` — Σ of ``income`` (≥ 0).
    * ``net_paise`` — ``income_paise + expense_paise`` (server-computed so the
      client can't drift on the sign). ``transfer`` is excluded entirely (it
      moves money between the user's own accounts — not income or spend).
    """

    start: date
    end: date
    income_paise: int
    expense_paise: int
    net_paise: int


class TaggingStatsResponse(BaseModel):
    """Envelope for ``GET /api/v1/dashboards/tagging-stats`` — two distinct F3
    health metrics (PRD §Success-metrics), neither a replacement for the other:
    ``acceptance_rate`` (of what we suggested, did it stick?) and
    ``coverage_rate`` (of what we imported, did we suggest anything at all? —
    the ≥80% pre-tag bar).

    Board-only (``confirmed_at IS NOT NULL``). ``acceptance_rate``'s denominator
    is rows the import auto-tagged to a **still-live** category (``auto_category_id``
    points at a non-archived bucket); ``kept`` is the subset whose final
    ``category_id`` still equals that suggestion (final-state semantics — a
    change-then-change-back counts as kept). Rows whose frozen suggestion
    points at a since-archived category are excluded from both numerator and
    denominator (current-health semantics — see the ``tagging_stats`` route +
    ADR-0004). ``acceptance_rate`` is ``None`` when ``total_auto_tagged == 0``
    to distinguish "no data yet" from a genuine 0% — the UI shows "—", not "0%".
    ``rules_count`` is the size of the user's ``merchant_tag_map`` (context for
    the rate, not part of it).

    ``imported_total`` is the count of board rows imported (``source ==
    "import"``); ``pre_tagged`` is the subset auto-tagged to a still-live
    category — the same population as ``total_auto_tagged``, exposed under its
    own name because here it is the coverage *numerator*, not the acceptance
    *denominator*. ``coverage_rate`` is ``None`` when ``imported_total == 0``
    (same "no data" vs "0%" contract).
    """

    total_auto_tagged: int
    kept: int
    acceptance_rate: float | None
    rules_count: int
    imported_total: int
    pre_tagged: int
    coverage_rate: float | None


#: Account types whose ``balance_paise`` is **excluded** from ``net_worth_paise``.
#: One home for the policy, so a second net-worth reader (the un-built over-time
#: series) cannot derive a third answer — it lives here, beside the two docstrings
#: that state the rule, rather than inline in the route that happens to read it.
#:
#: * ``credit_card`` — a **spend channel**, not a liability. Bill payments are never
#:   recorded, so its balance is lifetime accumulated spend rather than real
#:   outstanding debt; folding it in (even clamped) would subtract a full month's
#:   card spend from net worth without bound. Its calendar-YTD spend rides along on
#:   ``spend_ytd_paise`` instead. Its balance is legitimately non-zero.
#: * ``investment`` — a **placeholder**. Holdings live in ``instruments`` /
#:   ``investment_transactions``, never on an ``Account`` (PRD §F6/§F7), and
#:   ``transactions.py`` already refuses both transactions and transfers on the
#:   type. Counting an opening balance here would double-count the same money once
#:   it is itemised, which is why ``AccountCreate`` pins that balance to 0.
#:
#: Adding a member means deciding which of those two shapes it is — and the
#: create-time guard that follows from the choice. ``AccountTypeStr`` is
#: exhaustively bucketed by
#: ``tests/api/test_accounts.py::test_every_account_type_declares_a_net_worth_bucket``,
#: which fails until a new type is classified.
NET_WORTH_EXCLUDED_TYPES: frozenset[AccountTypeStr] = frozenset({"credit_card", "investment"})


class AccountBalanceRow(BaseModel):
    """One account's current balance for the Overview accounts panel.

    ``balance_paise`` is signed: ``opening_balance_paise`` plus the board-only
    (``confirmed_at IS NOT NULL``) signed sum of the account's transactions. A
    bank / cash account is normally positive. For a credit card this is the
    **all-time** signed sum (kept for reconciliation), but a credit card is
    treated as a *spend channel*, not a liability — bill payments aren't
    recorded, so its ``balance_paise`` is lifetime accumulated spend, not real
    debt. The UI does **not** show it as "owed"; it shows ``spend_ytd_paise``
    instead, and net worth excludes credit cards entirely. An ``investment``
    account is excluded too, for the opposite reason — it is a placeholder whose
    balance is pinned to 0 at create, because the money it would hold is already
    counted as holdings. Both exclusions come from
    :data:`NET_WORTH_EXCLUDED_TYPES`; this row still reports the raw signed
    balance for every type, excluded or not.

    ``spend_ytd_paise`` is the **signed net** Σ(spend) over the
    calendar year-to-date window (Jan 1 of the requested month's year → end of
    that month). It is **populated only for ``credit_card`` rows**; ``None`` for
    every other type (``None`` pins "not applicable" vs a genuine ₹0 spend). It
    is **not** guaranteed ≤ 0 — a refund-dominant window is legitimately
    positive and is never clamped server-side (same rule as
    ``expense_paise`` / ``SpendCategoryPeriodTotal``); the /dashboard accounts
    panel floors it to a non-negative "spent this year" magnitude for display.

    ``archived`` flags soft-deleted accounts. They are **included** in the
    Overview (and in net worth) — a closed account that still holds a balance is
    still part of net worth — so the frontend can dim or group them rather than
    have the figure silently change on archive.
    """

    account_id: int
    name: str
    type: AccountTypeStr
    currency: CurrencyStr
    balance_paise: int
    spend_ytd_paise: int | None
    gross_spend_ytd_paise: int | None = None
    refund_ytd_paise: int | None = None
    cashback_ytd_paise: int | None = None
    archived: bool


class OverviewResponse(BaseModel):
    """Envelope for ``GET /api/v1/dashboards/overview`` — the Financial Overview
    home (PRD §F8 view 1 + view 4).

    ``period`` echoes the requested month or year (stale-cache verification, cf.
    ``SpendByCategoryResponse``). Money fields are integer paise:

    * ``net_worth_paise`` — ``Σ(contributing accounts[].balance_paise) +
      portfolio_value_paise`` (server-computed so the client can't drift), where
      "contributing" means every type not in :data:`NET_WORTH_EXCLUDED_TYPES` —
      today ``bank`` and ``cash``. Credit cards are **excluded** because they're
      spend channels, not liabilities (bill payments aren't recorded, so a CC
      balance is accumulated spend, not real debt); each CC row instead carries
      ``spend_ytd_paise`` (its calendar-YTD spend). Investment accounts are
      **excluded** because they're placeholders whose value is already counted as
      ``portfolio_value_paise``. See ``AccountBalanceRow``.
    * ``portfolio_value_paise`` — INR rollup of current value over holdings
      **with** a NAV; null-NAV holdings count as 0 (never their cost). USD
      holdings convert at today's cached USD→INR rate (PRD §F7/F8).
    * ``fx_unavailable_count`` — priced USD holdings excluded from the rollup
      because no FX rate is cached (honesty flag, cf. ``PortfolioSummary`` /
      ``HoldingsValueRollup`` — never silently shrinks net worth).
    * ``income_paise`` / ``expense_paise`` / ``net_paise`` — the requested
      period's totals, identical in meaning to ``PeriodTotalsResponse``
      (``expense_paise`` is the signed Σ(spend) ≤ 0; ``net = income +
      expense``).
    """

    period: str
    net_worth_paise: int
    portfolio_value_paise: int
    fx_unavailable_count: int
    income_paise: int
    expense_paise: int
    net_paise: int
    accounts: list[AccountBalanceRow]
