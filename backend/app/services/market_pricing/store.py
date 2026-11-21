"""Writes: instruments, tracking, price upserts, coverage bookkeeping."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from itertools import batched
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.orm import Session

from app.models.market_pricing import (
    COVERAGE_DEAD,
    COVERAGE_ERROR,
    COVERAGE_NOT_FOUND,
    COVERAGE_OK,
    COVERAGE_PENDING,
    JOB_BACKFILL,
    MarketCorporateAction,
    MarketInstrument,
    MarketInstrumentPin,
    MarketPriceCoverage,
    MarketPriceHistory,
)

from . import jobs
from .providers.base import FetchResult, PricePoint, SplitEvent
from .symbols import resolve_price_key
from .util import utcnow


@dataclass(frozen=True, slots=True)
class TrackRequest:
    price_key: str
    first_trade_date: date | None = None
    amfi_code: str | None = None  # skips the ISIN lookup for mutual funds
    weight: float | None = None  # e.g. current holding value; bigger is fetched first


def get_instrument(session: Session, price_key: str) -> MarketInstrument | None:
    try:
        canonical = resolve_price_key(price_key).price_key
    except ValueError:
        return None
    return session.scalars(
        select(MarketInstrument).where(MarketInstrument.price_key == canonical)
    ).first()


def ensure_instrument(
    session: Session, price_key: str, *, amfi_code: str | None = None
) -> MarketInstrument:
    """Fetch or create the instrument (and its empty coverage row)."""
    resolved = resolve_price_key(price_key)
    instrument = session.scalars(
        select(MarketInstrument).where(MarketInstrument.price_key == resolved.price_key)
    ).first()
    if instrument is None:
        instrument = MarketInstrument(
            price_key=resolved.price_key,
            asset_class=resolved.asset_class,
            currency=resolved.currency,
            source=resolved.source,
            provider_symbol=resolved.provider_symbol,
            created_at=utcnow(),
        )
        instrument.coverage = MarketPriceCoverage(status=COVERAGE_PENDING)
        session.add(instrument)
        session.flush()
    if amfi_code and instrument.asset_class == "mf" and not instrument.provider_symbol:
        instrument.provider_symbol = str(amfi_code).strip()
    return instrument


def needs_backfill(coverage: MarketPriceCoverage, first_trade_date: date | None = None) -> bool:
    """Should a full-history fetch be queued for this instrument?"""
    if coverage.status in (COVERAGE_NOT_FOUND, COVERAGE_DEAD):
        return False  # upstream has nothing (more) to give; retry is manual
    if coverage.covered_from is None:
        return True
    return (
        first_trade_date is not None
        and first_trade_date < coverage.covered_from
        and not coverage.inception_reached
    )


def track(
    session: Session,
    requests: Iterable[TrackRequest | str],
    *,
    base_priority: int = jobs.PRIORITY_IMPORT,
    now: datetime | None = None,
) -> list[MarketInstrument]:
    """Register instruments and queue backfills for whatever is not cached yet.

    Safe to call on every import and every edit: it never touches the network,
    and re-tracking an instrument that is already covered is a no-op.
    """
    reqs = [TrackRequest(r) if isinstance(r, str) else r for r in requests]
    weighted = sorted(
        (i for i, r in enumerate(reqs) if r.weight is not None),
        key=lambda i: -(reqs[i].weight or 0.0),
    )
    boost = {index: 1 + max(0, 48 - position) for position, index in enumerate(weighted)}

    instruments: list[MarketInstrument] = []
    for index, req in enumerate(reqs):
        instrument = ensure_instrument(session, req.price_key, amfi_code=req.amfi_code)
        instrument.unreferenced_since = None
        if not instrument.upstream_dead and needs_backfill(
            instrument.coverage, req.first_trade_date
        ):
            jobs.enqueue(
                session,
                instrument,
                kind=JOB_BACKFILL,
                priority=base_priority + boost.get(index, 0),
                now=now,
            )
        instruments.append(instrument)
    return instruments


def pin(session: Session, price_key: str, reason: str = "benchmark") -> MarketInstrument:
    """Keep an instrument (benchmark index, FX pair) regardless of holdings."""
    instrument = track(session, [price_key])[0]
    if session.get(MarketInstrumentPin, (instrument.id, reason)) is None:
        session.add(MarketInstrumentPin(instrument_id=instrument.id, reason=reason))
        session.flush()
    return instrument


def unpin(session: Session, price_key: str, reason: str | None = None) -> int:
    instrument = get_instrument(session, price_key)
    if instrument is None:
        return 0
    stmt = delete(MarketInstrumentPin).where(MarketInstrumentPin.instrument_id == instrument.id)
    if reason is not None:
        stmt = stmt.where(MarketInstrumentPin.reason == reason)
    res = cast("CursorResult[Any]", session.execute(stmt))
    return res.rowcount or 0


def pinned_ids(session: Session) -> set[int]:
    return set(session.scalars(select(MarketInstrumentPin.instrument_id)).all())


def _dialect_insert(session: Session) -> Any:
    name = session.get_bind().dialect.name
    if name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        raise NotImplementedError(f"upsert is not implemented for {name}")
    return insert


def upsert_prices(
    session: Session,
    instrument_id: int,
    points: Sequence[PricePoint],
    *,
    batch_size: int = 500,
) -> int:
    """Insert new rows, overwrite changed ones, leave identical ones untouched.

    Returns the number of rows actually written, so re-fetching an unchanged
    series costs no page writes and no WAL growth.
    """
    insert = _dialect_insert(session)
    table = MarketPriceHistory.__table__
    written = 0
    for chunk in batched(points, batch_size, strict=False):
        stmt = insert(table).values(
            [
                {"instrument_id": instrument_id, "nav_date": p.day, "nav_micros": p.nav_micros}
                for p in chunk
            ]
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[table.c.instrument_id, table.c.nav_date],
            set_={"nav_micros": stmt.excluded.nav_micros},
            where=table.c.nav_micros != stmt.excluded.nav_micros,
        )
        res = cast("CursorResult[Any]", session.execute(stmt))
        written += res.rowcount or 0
    return written


def record_splits(
    session: Session, instrument_id: int, splits: Iterable[SplitEvent], source: str
) -> int:
    """Store split events not seen before. Returns how many were new."""
    known = set(
        session.scalars(
            select(MarketCorporateAction.action_date).where(
                MarketCorporateAction.instrument_id == instrument_id,
                MarketCorporateAction.kind == "split",
            )
        ).all()
    )
    added = 0
    for split in splits:
        if split.day in known:
            continue
        session.add(
            MarketCorporateAction(
                instrument_id=instrument_id,
                action_date=split.day,
                kind="split",
                numerator=float(split.numerator),
                denominator=float(split.denominator),
                source=source,
            )
        )
        known.add(split.day)
        added += 1
    return added


def apply_fetch_result(
    session: Session,
    instrument: MarketInstrument,
    result: FetchResult,
    *,
    full_history: bool,
    now: datetime | None = None,
) -> int:
    """Persist a successful fetch and update coverage. Returns rows written."""
    now = now or utcnow()
    written = upsert_prices(session, instrument.id, result.points)
    record_splits(session, instrument.id, result.splits, instrument.source)
    if result.currency and result.currency != instrument.currency:
        instrument.currency = result.currency  # trust the exchange over our guess

    coverage = instrument.coverage
    if result.points:
        first, last = result.points[0].day, result.points[-1].day
        coverage.covered_from = min(coverage.covered_from or first, first)
        coverage.covered_to = max(coverage.covered_to or last, last)
    if full_history:
        coverage.inception_reached = True
    if coverage.covered_to is not None:
        coverage.status = COVERAGE_OK
        instrument.upstream_dead = False
    coverage.last_attempt_at = now
    coverage.last_success_at = now
    coverage.consecutive_failures = 0
    coverage.last_error = None
    return written


def record_point(
    session: Session,
    instrument: MarketInstrument,
    point: PricePoint,
    *,
    now: datetime | None = None,
) -> int:
    """Persist a single fresh observation (the AMFI bulk path)."""
    return apply_fetch_result(
        session, instrument, FetchResult(points=[point]), full_history=False, now=now
    )


def mark_not_found(
    session: Session,
    instrument: MarketInstrument,
    error: str,
    *,
    dead_after: int = 3,
    now: datetime | None = None,
) -> bool:
    """Upstream does not know the symbol. Returns True if it is now flagged dead."""
    coverage = instrument.coverage
    coverage.last_attempt_at = now or utcnow()
    coverage.last_error = error[:500]
    if coverage.covered_to is None:
        coverage.status = COVERAGE_NOT_FOUND
        return False
    coverage.consecutive_failures += 1
    if coverage.consecutive_failures >= dead_after:
        instrument.upstream_dead = True
        coverage.status = COVERAGE_DEAD
        return True
    return False


def revive(
    session: Session, price_key: str, *, now: datetime | None = None
) -> MarketInstrument | None:
    """Clear a not_found/dead verdict and queue a fresh full fetch."""
    instrument = get_instrument(session, price_key)
    if instrument is None:
        return None
    coverage = instrument.coverage
    instrument.upstream_dead = False
    coverage.consecutive_failures = 0
    coverage.status = COVERAGE_OK if coverage.covered_to is not None else COVERAGE_PENDING
    jobs.enqueue(session, instrument, kind=JOB_BACKFILL, priority=jobs.PRIORITY_IMPORT, now=now)
    return instrument


def mark_error(
    session: Session, instrument: MarketInstrument, error: str, *, now: datetime | None = None
) -> None:
    coverage = instrument.coverage
    if coverage.covered_to is None:
        coverage.status = COVERAGE_ERROR
    coverage.consecutive_failures += 1
    coverage.last_attempt_at = now or utcnow()
    coverage.last_error = error[:500]
