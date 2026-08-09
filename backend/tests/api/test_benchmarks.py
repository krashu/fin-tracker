"""API tests for the benchmark routes (PRD §F8 view 5).

* ``GET /api/v1/benchmarks`` — active-only, id-ascending, ``BenchmarkRead`` shape.
* ``POST /api/v1/benchmarks/refresh`` — response shape, commit, degrade-to-warning.

Route-level only: the mfapi fetch/parse is unit-tested in
``tests/services/test_benchmark_service.py`` and stubbed here (the route builds its
own ``httpx.Client`` internally). Benchmarks are global reference data (no user
scoping), so there are no ownership tests.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Benchmark, BenchmarkNav, User
from app.services.benchmark_service import BenchmarkRefreshResult

_LIST_URL = "/api/v1/benchmarks"
_REFRESH_URL = "/api/v1/benchmarks/refresh"


def test_list_benchmarks_active_only_id_ordered(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    # Insert order fixes ids: active1 (1), archived (2), active2 (3).
    session.add_all(
        [
            Benchmark(name="Nifty 50 Index", kind="index_fund", amfi_code="120716", currency="INR"),
            Benchmark(
                name="Retired Index",
                kind="index_fund",
                amfi_code="999999",
                currency="INR",
                archived_at=datetime.now(UTC),
            ),
            Benchmark(name="Sensex Index", kind="index_fund", amfi_code="119063", currency="INR"),
        ]
    )
    session.commit()

    resp = client.get(_LIST_URL)
    assert resp.status_code == 200
    body = resp.json()
    # Archived excluded; remaining are id-ascending.
    assert [b["name"] for b in body] == ["Nifty 50 Index", "Sensex Index"]
    assert set(body[0].keys()) == {
        "id",
        "name",
        "kind",
        "amfi_code",
        "currency",
        "inception_date",
    }


def test_list_benchmarks_empty(client: TestClient, seeded_user: User) -> None:
    resp = client.get(_LIST_URL)
    assert resp.status_code == 200
    assert resp.json() == []


def test_refresh_benchmarks_happy_path(
    client: TestClient, seeded_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _stub(session: Session, **kwargs: object) -> BenchmarkRefreshResult:
        return BenchmarkRefreshResult(
            benchmarks_refreshed=1, navs_inserted=5, fetch_errors=0, warnings=[]
        )

    monkeypatch.setattr("app.api.v1.benchmarks.refresh_benchmark_navs", _stub)
    resp = client.post(_REFRESH_URL)
    assert resp.status_code == 200
    assert resp.json() == {
        "benchmarks_refreshed": 1,
        "navs_inserted": 5,
        "fetch_errors": 0,
        "warnings": [],
    }


def test_refresh_benchmarks_source_down_is_200_warning(
    client: TestClient, seeded_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _stub(session: Session, **kwargs: object) -> BenchmarkRefreshResult:
        return BenchmarkRefreshResult(
            benchmarks_refreshed=0, navs_inserted=0, fetch_errors=1, warnings=["mfapi timeout"]
        )

    monkeypatch.setattr("app.api.v1.benchmarks.refresh_benchmark_navs", _stub)
    resp = client.post(_REFRESH_URL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["fetch_errors"] == 1
    assert body["warnings"] == ["mfapi timeout"]


def test_refresh_benchmarks_requires_auth(unauth_client: TestClient) -> None:
    """The refresh is a global-reference write — it must reject an unauthenticated
    caller with 401 (mirrors POST /instruments/refresh-navs). The Origin header the
    fixture sets clears the CSRF gate, so 401 is the auth layer, not CSRF (403)."""
    resp = unauth_client.post(_REFRESH_URL)
    assert resp.status_code == 401


def test_refresh_benchmarks_commits(
    client: TestClient,
    seeded_user: User,
    session: Session,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route owns the commit: a NAV row the service writes via the passed
    session is durable after the response."""
    bench = Benchmark(name="Nifty 50 Index", kind="index_fund", amfi_code="120716", currency="INR")
    session.add(bench)
    session.commit()
    bench_id = bench.id

    def _stub(session: Session, **kwargs: object) -> BenchmarkRefreshResult:
        session.add(
            BenchmarkNav(benchmark_id=bench_id, nav_date=date(2026, 1, 1), nav=Decimal("250"))
        )
        return BenchmarkRefreshResult(benchmarks_refreshed=1, navs_inserted=1)

    monkeypatch.setattr("app.api.v1.benchmarks.refresh_benchmark_navs", _stub)
    resp = client.post(_REFRESH_URL)
    assert resp.status_code == 200
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(BenchmarkNav)) == 1
