"""Benchmark routes (PRD §F8 view 5).

* ``GET /api/v1/benchmarks`` — list active (non-archived) benchmarks; the picker's
  only source (the frontend renders whatever this returns, no codes hardcoded client-side).
* ``POST /api/v1/benchmarks/refresh`` — backfill ``benchmark_nav`` from mfapi. The
  seed-time, **cold** sibling of the ``GET /portfolio/performance`` read (which only
  reads the cache). A source failure degrades to a counted warning, not an error.

Benchmarks are global reference data (not per-user), so the read is public and the refresh
writes a shared cache. The refresh is a state-changing external-HTTP + DB write, so it still
requires an authenticated caller (any logged-in user, mirroring ``POST /instruments/refresh-navs``)
— "must be signed in", not per-user scoping. The catalog is migration-seeded and read-only in
v1 (no CRUD), so these are the whole surface.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import CurrentUserId, SessionDep
from app.core.config import get_settings
from app.models import Benchmark
from app.schemas import BenchmarkRead, BenchmarkRefreshSummary
from app.services.benchmark_service import refresh_benchmark_navs

router = APIRouter(prefix="/benchmarks", tags=["benchmarks"])


@router.get("", response_model=list[BenchmarkRead])
def list_benchmarks(session: SessionDep) -> list[Benchmark]:
    stmt = select(Benchmark).where(Benchmark.archived_at.is_(None)).order_by(Benchmark.id.asc())
    return list(session.scalars(stmt))


@router.post("/refresh", response_model=BenchmarkRefreshSummary)
def refresh_benchmarks_endpoint(session: SessionDep, _: CurrentUserId) -> BenchmarkRefreshSummary:
    """Backfill benchmark NAV history from mfapi (PRD §F8 view 5). Seed-time trigger.

    Synchronous + manual (no scheduler in v1). Fetches every active benchmark's history
    and caches new dates; a slow / unreachable scheme becomes a counted warning.
    """
    settings = get_settings()
    with httpx.Client(timeout=settings.nav_fetch_timeout_secs) as client:
        result = refresh_benchmark_navs(
            session, client=client, mfapi_base_url=settings.mfapi_base_url
        )
    session.commit()
    return BenchmarkRefreshSummary.model_validate(result)
