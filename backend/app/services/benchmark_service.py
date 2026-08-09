"""Benchmark NAV backfill (PRD §F8 view 5) — fill ``benchmark_nav`` from mfapi.

Seed-time only: this populates the per-fund NAV *history* the scalar-alpha read
(``performance_service``) replays cashflows against. It is **never** called on the
``GET /portfolio/performance`` hot path — the read touches only the cached rows.

Resilient like ``nav_snapshot_service``: one benchmark whose mfapi fetch/parse fails is
counted + warned and the rest proceed (no mass-failure on a single bad scheme). Idempotent:
existing ``(benchmark_id, nav_date)`` rows are skipped, so a re-run inserts only new dates.
Writes go through :func:`app.core.db_errors.insert_skip_existing` — the shared
dialect-aware ``ON CONFLICT DO NOTHING`` (SQLite v1 → Postgres v2), also used by
``fx_service`` — so a concurrent refresh can never crash on the unique index. The
caller owns the commit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db_errors import insert_skip_existing
from app.core.log_config import get_logger
from app.models import Benchmark, BenchmarkNav
from app.parsers import MfApiNavRow, MfApiParseError, parse_mfapi_navs

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BenchmarkRefreshResult:
    """Summary of one backfill run. Counts + PII-safe warnings only.

    ``benchmarks_refreshed`` = benchmarks whose history fetched + parsed; ``navs_inserted``
    = new NAV rows written across all of them; ``fetch_errors`` = benchmarks whose mfapi
    fetch/parse failed (history left untouched).
    """

    benchmarks_refreshed: int = 0
    navs_inserted: int = 0
    fetch_errors: int = 0
    warnings: list[str] = field(default_factory=list)


def refresh_benchmark_navs(
    session: Session,
    *,
    client: httpx.Client,
    mfapi_base_url: str,
    benchmark_ids: list[int] | None = None,
) -> BenchmarkRefreshResult:
    """Fetch + cache NAV history for the active benchmarks (PRD §F8 view 5).

    ``client`` + ``mfapi_base_url`` are injected so the route owns config and tests use
    ``httpx.MockTransport``. ``benchmark_ids`` optionally narrows to a subset (else all
    non-archived benchmarks).
    """
    stmt = select(Benchmark).where(Benchmark.archived_at.is_(None))
    if benchmark_ids is not None:
        stmt = stmt.where(Benchmark.id.in_(benchmark_ids))
    benchmarks = list(session.scalars(stmt.order_by(Benchmark.id)))

    warnings: list[str] = []
    benchmarks_refreshed = navs_inserted = fetch_errors = 0

    for b in benchmarks:
        parsed, cause = _fetch_history(client, mfapi_base_url, b.amfi_code)
        if parsed is None:
            fetch_errors += 1
            warnings.append(f"benchmark {b.id} ({b.amfi_code}): NAV history unreachable — {cause}")
            continue
        rows, parse_warnings = parsed
        warnings.extend(parse_warnings)
        navs_inserted += _apply_history(session, b.id, rows)
        benchmarks_refreshed += 1

    result = BenchmarkRefreshResult(
        benchmarks_refreshed=benchmarks_refreshed,
        navs_inserted=navs_inserted,
        fetch_errors=fetch_errors,
        warnings=warnings,
    )
    logger.info(
        "benchmark_navs_refreshed",
        benchmarks_refreshed=benchmarks_refreshed,
        navs_inserted=navs_inserted,
        fetch_errors=fetch_errors,
    )
    return result


def _fetch_history(
    client: httpx.Client, base_url: str, amfi_code: str
) -> tuple[tuple[list[MfApiNavRow], list[str]] | None, str | None]:
    """GET + parse one fund's mfapi history.

    Returns ``(parsed, None)`` on success or ``(None, cause)`` on any source failure —
    the trimmed cause is surfaced in the caller's warning so a refresh that fetched
    nothing isn't opaque (e.g. an SSL/cert error behind a corporate proxy).
    """
    try:
        resp = client.get(f"{base_url}/{amfi_code}")
        resp.raise_for_status()
        return parse_mfapi_navs(resp.content), None
    except (httpx.HTTPError, MfApiParseError) as e:
        logger.warning("mfapi_fetch_failed", amfi_code=amfi_code, error=str(e))
        return None, str(e)[:160]


def _apply_history(session: Session, benchmark_id: int, rows: list[MfApiNavRow]) -> int:
    """Insert NAV rows not already cached for ``benchmark_id``. Returns rows inserted."""
    by_date: dict[date, Decimal] = {}
    for r in rows:
        # mfapi shouldn't duplicate a date; if it does, keep the first (newest-first order).
        by_date.setdefault(r.nav_date, r.nav)
    existing: set[date] = set(
        session.scalars(
            select(BenchmarkNav.nav_date).where(BenchmarkNav.benchmark_id == benchmark_id)
        )
    )
    new_rows: list[dict[str, object]] = [
        {"benchmark_id": benchmark_id, "nav_date": d, "nav": nav}
        for d, nav in by_date.items()
        if d not in existing
    ]
    if not new_rows:
        return 0
    insert_skip_existing(
        session,
        BenchmarkNav,
        new_rows,
        conflict_cols=["benchmark_id", "nav_date"],
        label="benchmark_nav",
    )
    return len(new_rows)
