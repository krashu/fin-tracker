"""Daily refresh: keep active instruments current at minimal upstream cost.

Mutual funds   ONE request total: AMFI's NAVAll.txt carries the latest NAV of
               every scheme. mfapi is only used to backfill or to close a gap
               (e.g. the server was off for a week).
Everything     One short-range Yahoo request per *active* ticker, queued at
else           background priority and paced by the worker.

"Active" = currently held by some household, or pinned (benchmark / FX).
Fully sold instruments keep their history but stop being refreshed once it
reaches their last transaction date.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core import clock
from app.core.log_config import get_logger
from app.models.market_pricing import (
    COVERAGE_NOT_FOUND,
    JOB_BACKFILL,
    JOB_REFRESH,
    MarketInstrument,
)

from . import jobs, store
from .config import Settings
from .providers.amfi import AmfiBulkClient, AmfiRecord
from .providers.base import PricePoint, ProviderError, RateLimited
from .ratelimit import HostGate
from .symbols import resolve_price_key
from .usage import UsageSource
from .util import utcnow, weekdays_between

logger = get_logger(__name__)


@dataclass(slots=True)
class RefreshReport:
    newly_tracked: list[str] = field(default_factory=list)
    active: int = 0
    skipped_closed: int = 0
    amfi_ok: bool | None = None  # None = no mutual funds needed it
    mf_updated_from_bulk: int = 0
    mf_gap_jobs: int = 0
    mf_missing_from_bulk: list[str] = field(default_factory=list)
    refresh_jobs: int = 0
    backfill_jobs: int = 0
    skipped_fresh: int = 0

    def summary(self) -> str:
        return (
            f"active={self.active} bulk_mf={self.mf_updated_from_bulk} "
            f"mf_gap_jobs={self.mf_gap_jobs} refresh_jobs={self.refresh_jobs} "
            f"backfill_jobs={self.backfill_jobs} fresh={self.skipped_fresh} "
            f"new={len(self.newly_tracked)} amfi_ok={self.amfi_ok}"
        )


def sync_usage(session: Session, usage: UsageSource, report: RefreshReport | None = None) -> None:
    """Make sure every instrument referenced by a transaction is tracked."""
    known = set(session.scalars(select(MarketInstrument.price_key)).all())
    requests = []
    for item in usage.usages(session):
        try:
            canonical = resolve_price_key(item.price_key).price_key
        except ValueError:
            logger.warning("ignoring_unsupported_price_key", price_key=item.price_key)
            continue
        if report is not None and canonical not in known:
            report.newly_tracked.append(canonical)
        requests.append(store.TrackRequest(item.price_key, item.first_trade_date))
    store.track(session, requests, base_priority=jobs.PRIORITY_BACKGROUND)


def _active_instruments(
    session: Session, usage: UsageSource | None, report: RefreshReport
) -> list[MarketInstrument]:
    instruments = session.scalars(
        select(MarketInstrument)
        .where(MarketInstrument.upstream_dead.is_(False))
        .order_by(MarketInstrument.id)
    ).all()
    if usage is None:
        return list(instruments)

    by_key = {}
    for item in usage.usages(session):
        try:
            by_key[resolve_price_key(item.price_key).price_key] = item
        except ValueError:
            continue
    pinned = store.pinned_ids(session)
    active: list[MarketInstrument] = []
    for instrument in instruments:
        item = by_key.get(instrument.price_key)
        if instrument.id in pinned or (item is not None and item.is_open):
            active.append(instrument)
        elif item is not None:
            covered_to = instrument.coverage.covered_to
            if covered_to is None or covered_to < item.last_activity_date:
                active.append(instrument)
            else:
                report.skipped_closed += 1
    return active


def run_daily_refresh(
    session_factory: sessionmaker[Session],
    *,
    gate: HostGate,
    usage: UsageSource | None = None,
    amfi: AmfiBulkClient | None = None,
    settings: Settings | None = None,
    today: date | None = None,
    now: datetime | None = None,
) -> RefreshReport:
    settings = settings or Settings()
    now = now or utcnow()
    today = today or clock.today()
    report = RefreshReport()
    min_gap = timedelta(hours=settings.min_refresh_interval_h)

    # Phase 1 (DB only): sync the tracked set and see whether any fund needs AMFI.
    with session_factory.begin() as session:
        if usage is not None:
            sync_usage(session, usage, report)
        active = _active_instruments(session, usage, report)
        report.active = len(active)
        needs_amfi = any(
            i.asset_class == "mf" and i.coverage.covered_to is not None for i in active
        )
        amfi_paused = gate.paused_until(session, "amfi") is not None

    # Phase 2 (network, no transaction held open): one bulk download for all funds.
    bulk: dict[str, AmfiRecord] | None = None
    bulk_by_isin: dict[str, AmfiRecord] = {}
    if needs_amfi and amfi is not None and not amfi_paused:
        try:
            records = amfi.records()
            bulk = {r.scheme_code: r for r in records}
            bulk_by_isin = {isin: r for r in records for isin in r.isins}
            report.amfi_ok = True
        except ProviderError as exc:
            report.amfi_ok = False
            logger.warning("amfi_bulk_unavailable_falling_back", error=str(exc))
            if isinstance(exc, RateLimited):
                with session_factory.begin() as session:
                    gate.trip(session, "amfi", reason=str(exc), retry_after_s=exc.retry_after_s)
    elif needs_amfi:
        report.amfi_ok = False

    # Phase 3 (DB only): apply bulk NAVs, queue whatever still needs a request.
    with session_factory.begin() as session:
        if bulk is not None:
            gate.record_success(session, "amfi")
        active_ids = [i.id for i in active]
        instruments = (
            session.scalars(
                select(MarketInstrument)
                .where(MarketInstrument.id.in_(active_ids))
                .order_by(MarketInstrument.id)
            ).all()
            if active_ids
            else []
        )

        for instrument in instruments:
            coverage = instrument.coverage
            if coverage.covered_to is None:
                if coverage.status != COVERAGE_NOT_FOUND:
                    jobs.enqueue(session, instrument, kind=JOB_BACKFILL, now=now)
                    report.backfill_jobs += 1
                continue
            if coverage.covered_to >= today:
                report.skipped_fresh += 1
                continue

            if instrument.asset_class == "mf" and bulk is not None:
                record = None
                if instrument.provider_symbol:
                    record = bulk.get(instrument.provider_symbol)
                if record is None:
                    record = bulk_by_isin.get(instrument.price_key)
                if record is None or record.nav_micros is None or record.day is None:
                    report.mf_missing_from_bulk.append(instrument.price_key)
                    continue
                if not instrument.provider_symbol:
                    instrument.provider_symbol = record.scheme_code
                if record.day <= coverage.covered_to:
                    report.skipped_fresh += 1
                    continue
                if weekdays_between(coverage.covered_to, record.day) > 0:
                    jobs.enqueue(
                        session,
                        instrument,
                        kind=JOB_REFRESH,
                        range_start=coverage.covered_to,
                        now=now,
                    )
                    report.mf_gap_jobs += 1
                    continue
                store.record_point(
                    session, instrument, PricePoint(record.day, record.nav_micros), now=now
                )
                report.mf_updated_from_bulk += 1
                continue

            if coverage.last_success_at is not None and now - coverage.last_success_at < min_gap:
                report.skipped_fresh += 1
                continue
            jobs.enqueue(
                session,
                instrument,
                kind=JOB_REFRESH,
                range_start=coverage.covered_to - timedelta(days=settings.refresh_overlap_days),
                now=now,
            )
            report.refresh_jobs += 1

    logger.info("daily_refresh_completed", summary=report.summary())
    return report
