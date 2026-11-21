"""Tests for market pricing models, store, query, ratelimit, jobs, and worker (ADR-0021)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.models.market_pricing import (
    COVERAGE_OK,
    COVERAGE_PENDING,
)
from app.services.market_pricing import (
    FetchResult,
    HostGate,
    HostPolicy,
    InstrumentNotFound,
    PricePoint,
    PriceWorker,
    SplitEvent,
    StaticUsageSource,
    UnsupportedPriceKey,
    apply_fetch_result,
    collect_garbage,
    ensure_instrument,
    from_micros,
    get_instrument,
    pin,
    pinned_ids,
    price_on,
    resolve_price_key,
    split_adjust,
    split_factor,
    splits_for,
    to_micros,
    track,
    unpin,
    upsert_prices,
)


class SimClock:
    def __init__(self, start: datetime = datetime(2026, 11, 20, 6, 0, 0)):
        self.now = start
        self.mono = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.advance(seconds=seconds)

    def advance(self, **kwargs) -> None:
        delta = timedelta(**kwargs)
        self.now += delta
        self.mono += delta.total_seconds()


class FakeProvider:
    def __init__(self, name: str):
        self.name = name
        self.series: dict[str, list[PricePoint]] = {}
        self.splits: dict[str, list[SplitEvent]] = {}
        self.failures: dict[str, list[Exception]] = {}
        self.calls: list[tuple[str, date | None]] = []

    def fetch_history(self, symbol: str, start: date | None = None) -> FetchResult:
        self.calls.append((symbol, start))
        queue = self.failures.get(symbol) or self.failures.get("*")
        if queue:
            raise queue.pop(0)
        if symbol not in self.series:
            raise InstrumentNotFound(f"unknown symbol {symbol}")
        points = [p for p in self.series[symbol] if start is None or p.day >= start]
        return FetchResult(points=points, splits=list(self.splits.get(symbol, [])))


@pytest.fixture
def mem_engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def sessions(mem_engine):
    return sessionmaker(bind=mem_engine)


@pytest.fixture
def session(sessions):
    with sessions() as s:
        yield s


# ---------------------------------------------------------------------------
# Symbols & Money tests
# ---------------------------------------------------------------------------


def test_symbols_resolution():
    res = resolve_price_key("INF879O01027")
    assert res.price_key == "INF879O01027"
    assert res.asset_class == "mf"
    assert res.source == "mfapi"
    assert res.currency == "INR"

    res_nse = resolve_price_key("NSE:TCS")
    assert res_nse.provider_symbol == "TCS.NS"
    assert res_nse.asset_class == "equity"
    assert res_nse.currency == "INR"

    res_us = resolve_price_key("NASDAQ:AAPL")
    assert res_us.provider_symbol == "AAPL"
    assert res_us.currency == "USD"

    res_fx = resolve_price_key("FX:USDINR")
    assert res_fx.provider_symbol == "USDINR=X"
    assert res_fx.currency == "INR"

    res_idx = resolve_price_key("INDEX:NSEI")
    assert res_idx.provider_symbol == "^NSEI"
    assert res_idx.currency == "INR"

    with pytest.raises(UnsupportedPriceKey):
        resolve_price_key("UNKNOWN:FOO")

    with pytest.raises(UnsupportedPriceKey):
        resolve_price_key("INVALID_ISIN")


def test_money_conversions():
    assert to_micros("100.50") == 1_005_000
    assert to_micros(100) == 1_000_000
    assert to_micros(Fraction(1, 2)) == 5000
    assert from_micros(1_005_000) == pytest.approx(100.50)

    with pytest.raises(ValueError):
        to_micros("abc")


# ---------------------------------------------------------------------------
# Store & Query tests
# ---------------------------------------------------------------------------


def test_ensure_instrument_and_upsert(session: Session):
    inst = ensure_instrument(session, "NSE:INFY")
    assert inst.id is not None
    assert inst.coverage.status == COVERAGE_PENDING

    d1 = date(2026, 11, 1)
    d2 = date(2026, 11, 2)
    pts = [PricePoint(d1, 1500_0000), PricePoint(d2, 1520_0000)]
    written = upsert_prices(session, inst.id, pts)
    assert written == 2

    # Idempotent write: identical points write 0 rows
    assert upsert_prices(session, inst.id, pts) == 0

    # Query price_on LOCF
    p_on_1 = price_on(session, inst.id, d1)
    assert p_on_1 is not None and p_on_1.nav_micros == 1500_0000

    # Weekend / future LOCF
    p_on_weekend = price_on(session, inst.id, date(2026, 11, 3))
    assert p_on_weekend is not None and p_on_weekend.nav_micros == 1520_0000

    # Before earliest point
    p_before = price_on(session, inst.id, date(2026, 10, 31))
    assert p_before is None


def test_splits_and_adjustments(session: Session):
    inst = ensure_instrument(session, "NSE:SPLITCO")
    d1 = date(2026, 1, 10)
    split_date = date(2026, 1, 15)
    d2 = date(2026, 1, 20)

    res = FetchResult(
        points=[PricePoint(d1, 2000_0000), PricePoint(d2, 1000_0000)],
        splits=[SplitEvent(split_date, Fraction(2), Fraction(1))],
    )
    apply_fetch_result(session, inst, res, full_history=True)

    splits = splits_for(session, inst.id)
    assert len(splits) == 1
    assert split_factor(splits, d1, d2) == Fraction(2, 1)

    pts = [PricePoint(d1, 2000_0000), PricePoint(d2, 1000_0000)]
    adj = split_adjust(pts, splits, as_of=d2)
    # Price on d1 rebased to post-split basis: 2000 / 2 = 1000
    assert adj[0].nav_micros == 1000_0000
    assert adj[1].nav_micros == 1000_0000


def test_pinning(session: Session):
    inst = pin(session, "INDEX:NSEI", reason="benchmark")
    assert inst.id in pinned_ids(session)

    unpin(session, "INDEX:NSEI")
    assert inst.id not in pinned_ids(session)


# ---------------------------------------------------------------------------
# Rate Limiting & Circuit Breaker tests
# ---------------------------------------------------------------------------


def test_host_gate_circuit_breaker(session: Session):
    clock = SimClock()
    gate = HostGate(
        {"yahoo": HostPolicy(base_pause_s=100.0, max_pause_s=800.0)},
        clock=clock,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert gate.paused_until(session, "yahoo") is None

    # First trip: pauses for 100s
    paused_until = gate.trip(session, "yahoo", reason="429 Too Many Requests")
    assert paused_until == clock.now + timedelta(seconds=100)
    assert "yahoo" in gate.paused_hosts(session)

    # Second trip: doubles to 200s
    paused_until_2 = gate.trip(session, "yahoo", reason="429 Too Many Requests")
    assert paused_until_2 == clock.now + timedelta(seconds=200)

    # Success clears
    gate.record_success(session, "yahoo")
    assert gate.paused_until(session, "yahoo") is None


# ---------------------------------------------------------------------------
# Worker tests
# ---------------------------------------------------------------------------


def test_price_worker_drain(sessions, session: Session):
    clock = SimClock()
    gate = HostGate(clock=clock, monotonic=clock.monotonic, sleep=clock.sleep)
    provider = FakeProvider("yahoo")
    d1 = date(2026, 11, 10)
    provider.series["RELIANCE.NS"] = [PricePoint(d1, 2500_0000)]

    track(session, ["NSE:RELIANCE"], now=clock())
    session.commit()

    worker = PriceWorker(sessions, {"yahoo": provider}, gate, clock=clock)
    handled = worker.drain(max_jobs=5)
    assert handled == 1

    with sessions() as s:
        refreshed = get_instrument(s, "NSE:RELIANCE")
        assert refreshed is not None
        assert refreshed.coverage.status == COVERAGE_OK
        p = price_on(s, refreshed.id, d1)
        assert p is not None and p.nav_micros == 2500_0000


# ---------------------------------------------------------------------------
# Garbage Collection tests
# ---------------------------------------------------------------------------


def test_collect_garbage(session: Session):
    # Track an instrument that is not referenced
    track(session, ["NSE:ORPHAN"])
    session.commit()

    usage = StaticUsageSource([])
    t0 = date(2026, 1, 1)

    # Run GC: marks as unreferenced_since = t0
    rep1 = collect_garbage(session, usage, today=t0, grace_days=90, allow_empty_usage=True)
    assert "NSE:ORPHAN" in rep1.newly_unreferenced

    # 30 days later: still in grace period
    t1 = t0 + timedelta(days=30)
    rep2 = collect_garbage(session, usage, today=t1, grace_days=90, allow_empty_usage=True)
    assert "NSE:ORPHAN" in rep2.waiting

    # 91 days later: evicted
    t2 = t0 + timedelta(days=91)
    rep3 = collect_garbage(session, usage, today=t2, grace_days=90, allow_empty_usage=True)
    assert "NSE:ORPHAN" in rep3.evicted
    session.commit()

    assert get_instrument(session, "NSE:ORPHAN") is None


# ---------------------------------------------------------------------------
# MarketDataService Facade tests
# ---------------------------------------------------------------------------


def test_market_data_service_facade(session: Session):
    from app.services.market_data_service import (
        apply_locf_to_dates,
        from_nav_micros,
        get_nav_as_of,
        get_nav_history_series,
        record_market_nav,
        to_nav_micros,
        track_instrument,
    )

    assert to_nav_micros("123.4567") == 1234567
    assert from_nav_micros(1234567) == Decimal("123.4567")

    d1 = date(2026, 11, 10)
    d2 = date(2026, 11, 12)

    record_market_nav(session, "NSE:WIPRO", d1, 500_0000)
    session.commit()

    # LOCF lookup on exact date
    nav_d1 = get_nav_as_of(session, "NSE:WIPRO", d1)
    assert nav_d1 == 500_0000

    # LOCF lookup on later date carries forward
    nav_d2 = get_nav_as_of(session, "NSE:WIPRO", d2)
    assert nav_d2 == 500_0000

    # Date mapping
    locf_map = apply_locf_to_dates(session, "NSE:WIPRO", [d1, d1 + timedelta(days=1), d2])
    assert locf_map[d1] == 500_0000
    assert locf_map[d1 + timedelta(days=1)] == 500_0000
    assert locf_map[d2] == 500_0000

    # History series
    series = get_nav_history_series(session, "NSE:WIPRO", d1, d2)
    assert len(series) == 3
    assert [p.day for p in series] == [d1, d1 + timedelta(days=1), d2]

    # Track instrument
    inst = track_instrument(session, "NSE:WIPRO")
    assert inst.price_key == "NSE:WIPRO"


# ---------------------------------------------------------------------------
# Provider tests
# ---------------------------------------------------------------------------


def test_parse_navall():
    from app.services.market_pricing.providers.amfi import parse_navall

    raw_text = (
        "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;NAV;Date\n"
        "119551;INF209KA12Z1;INF209KA13Z9;ABSL Banking & PSU Debt Fund;111.0395;06-Mar-2026\n"
        "122639;INF879O01027;;PPFAS Flexi Cap Fund - Direct Plan - Growth;85.1234;06-Mar-2026\n"
    )
    records = parse_navall(raw_text)
    assert len(records) == 2
    rec1 = records[0]
    assert rec1.scheme_code == "119551"
    assert "INF209KA12Z1" in rec1.isins
    assert rec1.nav_micros == 111_0395
    assert rec1.day == date(2026, 3, 6)

    rec2 = records[1]
    assert rec2.scheme_code == "122639"
    assert rec2.isins == ("INF879O01027",)
    assert rec2.nav_micros == 85_1234


# ---------------------------------------------------------------------------
# Maintenance tests
# ---------------------------------------------------------------------------


def test_maintenance_sqlite_stats(mem_engine, tmp_path):
    from app.services.market_pricing.maintenance import (
        backup,
        checkpoint,
        db_stats,
        vacuum_if_fragmented,
    )

    stats = db_stats(mem_engine)
    assert "page_size" in stats
    assert "free_ratio" in stats

    checkpoint(mem_engine)
    assert not vacuum_if_fragmented(mem_engine, threshold=0.99)

    # Test file-based engine for VACUUM INTO backup
    file_db = tmp_path / "test.db"
    file_engine = create_engine(f"sqlite:///{file_db.as_posix()}")
    Base.metadata.create_all(file_engine)

    backup_dir = tmp_path / "backups"
    archive = backup(file_engine, backup_dir, keep=3)
    assert archive is not None
    assert archive.exists()
    file_engine.dispose()
