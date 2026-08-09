"""Service tests for the benchmark NAV backfill (PRD §F8 view 5).

Exercises ``refresh_benchmark_navs`` against an in-memory session with mfapi mocked via
``httpx.MockTransport`` (no network). Covers: history cached + NAV scaled correctly through
``PriceNative``; idempotent re-run inserts nothing; only missing dates inserted; a source
failure counted-not-raised (others proceed); ``benchmark_ids`` filter; archived skipped; and
a per-row parse warning propagated.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Benchmark, BenchmarkNav
from app.services.benchmark_service import refresh_benchmark_navs

_MFAPI_URL = "https://mfapi.test/mf"


def _body(scheme_code: str, entries: list[tuple[str, str]]) -> bytes:
    payload = {
        "meta": {"scheme_code": scheme_code},
        "data": [{"date": d, "nav": n} for d, n in entries],
        "status": "SUCCESS",
    }
    return json.dumps(payload).encode("utf-8")


def _serving(by_code: dict[str, list[tuple[str, str]]], *, down: frozenset[str] = frozenset()):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        code = str(request.url).rstrip("/").rsplit("/", 1)[-1]
        if code in down:
            return httpx.Response(503)
        if code not in by_code:
            return httpx.Response(404)
        return httpx.Response(200, content=_body(code, by_code[code]))

    return handler


def _benchmark(session: Session, *, name: str, amfi_code: str, **kw) -> Benchmark:  # type: ignore[no-untyped-def]
    b = Benchmark(name=name, kind="index_fund", amfi_code=amfi_code, currency="INR", **kw)
    session.add(b)
    session.flush()
    return b


def _run(session: Session, handler, **kw):  # type: ignore[no-untyped-def]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return refresh_benchmark_navs(session, client=client, mfapi_base_url=_MFAPI_URL, **kw)


def _cached(session: Session, benchmark_id: int) -> dict[date, Decimal]:
    rows = session.scalars(select(BenchmarkNav).where(BenchmarkNav.benchmark_id == benchmark_id))
    return {r.nav_date: r.nav for r in rows}


def test_caches_history_and_scales_nav(session: Session) -> None:
    b = _benchmark(session, name="Nifty 50", amfi_code="100")
    handler = _serving({"100": [("19-06-2026", "245.6789"), ("18-06-2026", "244.1200")]})

    result = _run(session, handler)

    assert result.benchmarks_refreshed == 1
    assert result.navs_inserted == 2
    assert result.fetch_errors == 0
    cached = _cached(session, b.id)
    # Round-trip through PriceNative (scaled int64) must preserve the exact Decimal.
    assert cached[date(2026, 6, 19)] == Decimal("245.6789")
    assert cached[date(2026, 6, 18)] == Decimal("244.1200")


def test_idempotent_rerun_inserts_zero(session: Session) -> None:
    b = _benchmark(session, name="Nifty 50", amfi_code="100")
    handler = _serving({"100": [("19-06-2026", "245.6789"), ("18-06-2026", "244.12")]})

    first = _run(session, handler)
    second = _run(session, handler)

    assert first.navs_inserted == 2
    assert second.navs_inserted == 0
    assert len(_cached(session, b.id)) == 2


def test_only_missing_dates_inserted(session: Session) -> None:
    b = _benchmark(session, name="Nifty 50", amfi_code="100")
    _run(session, _serving({"100": [("18-06-2026", "244.12")]}))  # cache one date
    # Feed now carries the old date + a newer one — only the new date is inserted.
    result = _run(session, _serving({"100": [("19-06-2026", "245.68"), ("18-06-2026", "244.12")]}))

    assert result.navs_inserted == 1
    assert set(_cached(session, b.id)) == {date(2026, 6, 18), date(2026, 6, 19)}


def test_source_failure_counted_not_raised(session: Session) -> None:
    ok = _benchmark(session, name="Nifty 50", amfi_code="100")
    bad = _benchmark(session, name="Down Fund", amfi_code="999")
    handler = _serving({"100": [("19-06-2026", "245.68")]}, down=frozenset({"999"}))

    result = _run(session, handler)

    assert result.benchmarks_refreshed == 1
    assert result.fetch_errors == 1
    assert result.navs_inserted == 1
    assert len(_cached(session, ok.id)) == 1
    assert _cached(session, bad.id) == {}
    # The warning now carries the real cause (the 503), not just the scheme code, so a
    # failed refresh is diagnosable from the API response — not only the server log.
    assert any("999" in w and "503" in w for w in result.warnings)


def test_benchmark_ids_filters(session: Session) -> None:
    b1 = _benchmark(session, name="Nifty 50", amfi_code="100")
    b2 = _benchmark(session, name="Nifty Next 50", amfi_code="200")
    handler = _serving({"100": [("19-06-2026", "245.68")], "200": [("19-06-2026", "60.5")]})

    result = _run(session, handler, benchmark_ids=[b1.id])

    assert result.benchmarks_refreshed == 1
    assert len(_cached(session, b1.id)) == 1
    assert _cached(session, b2.id) == {}


def test_archived_benchmark_skipped(session: Session) -> None:
    from datetime import UTC, datetime

    b = _benchmark(session, name="Old Fund", amfi_code="100", archived_at=datetime.now(UTC))
    result = _run(session, _serving({"100": [("19-06-2026", "245.68")]}))

    assert result.benchmarks_refreshed == 0
    assert _cached(session, b.id) == {}


def test_per_row_parse_warning_propagated(session: Session) -> None:
    b = _benchmark(session, name="Nifty 50", amfi_code="100")
    handler = _serving({"100": [("19-06-2026", "245.68"), ("18-06-2026", "N.A.")]})

    result = _run(session, handler)

    assert result.navs_inserted == 1  # the valid row only
    assert any("non-numeric nav" in w for w in result.warnings)
    assert set(_cached(session, b.id)) == {date(2026, 6, 19)}
