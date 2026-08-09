"""Benchmark request/response schemas (PRD §F8 view 5).

``BenchmarkRead`` is the picker list for ``GET /benchmarks`` (the frontend renders
whatever this returns — no scheme codes hardcoded client-side). ``BenchmarkRefreshSummary``
is the ``POST /benchmarks/refresh`` body, built from ``benchmark_service.BenchmarkRefreshResult``.
"""

from __future__ import annotations

from datetime import date as date_t

from pydantic import BaseModel, ConfigDict

from app.models.account import CurrencyStr
from app.models.benchmark import BenchmarkKindStr


class BenchmarkRead(BaseModel):
    """One curated index fund the user can compare against."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    kind: BenchmarkKindStr
    amfi_code: str
    currency: CurrencyStr
    inception_date: date_t | None


class BenchmarkRefreshSummary(BaseModel):
    """Response body of ``POST /api/v1/benchmarks/refresh`` (seed-time backfill).

    Counts from one mfapi backfill run + PII-safe warnings. Built from
    ``benchmark_service.BenchmarkRefreshResult`` (``from_attributes``).
    """

    model_config = ConfigDict(from_attributes=True)

    benchmarks_refreshed: int
    navs_inserted: int
    fetch_errors: int
    warnings: list[str]
