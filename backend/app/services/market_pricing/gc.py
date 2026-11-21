"""Garbage collection of price history nobody references any more.

An instrument is evicted only when ALL of these hold:

1. no transaction in any household references it (current balance is
   irrelevant: a fully sold position keeps its history),
2. it is not pinned (benchmark, FX pair),
3. upstream still serves it (dead instruments are irreplaceable, so kept),
4. it has been unreferenced for a full grace period (default 90 days), so
   "delete the statement, fix it, import again" never triggers a re-download.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core import clock
from app.core.log_config import get_logger
from app.models.market_pricing import (
    MarketCorporateAction,
    MarketInstrument,
    MarketInstrumentPin,
    MarketPriceCoverage,
    MarketPriceHistory,
    MarketPriceJob,
)

from . import store
from .symbols import resolve_price_key
from .usage import UsageSource

logger = get_logger(__name__)


@dataclass(slots=True)
class GcReport:
    referenced: int = 0
    newly_unreferenced: list[str] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)  # inside the grace period
    kept_dead: list[str] = field(default_factory=list)  # unreferenced but irreplaceable
    evicted: list[str] = field(default_factory=list)
    rows_deleted: int = 0
    aborted: str | None = None
    dry_run: bool = False


def collect_garbage(
    session: Session,
    usage: UsageSource,
    *,
    today: date | None = None,
    grace_days: int = 90,
    dry_run: bool = False,
    allow_empty_usage: bool = False,
) -> GcReport:
    today = today or clock.today()
    report = GcReport(dry_run=dry_run)

    referenced: set[str] = set()
    for item in usage.usages(session):
        try:
            referenced.add(resolve_price_key(item.price_key).price_key)
        except ValueError:
            continue
    pinned = store.pinned_ids(session)
    instruments = session.scalars(select(MarketInstrument).order_by(MarketInstrument.id)).all()

    if not referenced and instruments and not allow_empty_usage:
        report.aborted = (
            "usage source returned no instruments; refusing to mark anything "
            "(pass allow_empty_usage=True if the household really holds nothing)"
        )
        logger.warning("gc_aborted", reason=report.aborted)
        return report

    for instrument in instruments:
        key = instrument.price_key
        if key in referenced or instrument.id in pinned:
            report.referenced += 1
            if instrument.unreferenced_since is not None and not dry_run:
                instrument.unreferenced_since = None
            continue

        if instrument.unreferenced_since is None:
            report.newly_unreferenced.append(key)
            if not dry_run:
                instrument.unreferenced_since = today
            continue
        if instrument.upstream_dead:
            report.kept_dead.append(key)
            continue
        if (today - instrument.unreferenced_since).days < grace_days:
            report.waiting.append(key)
            continue

        rows = (
            session.scalar(
                select(func.count())
                .select_from(MarketPriceHistory)
                .where(MarketPriceHistory.instrument_id == instrument.id)
            )
            or 0
        )
        report.evicted.append(key)
        report.rows_deleted += rows
        if not dry_run:
            _evict(session, instrument.id)

    if report.evicted:
        logger.info(
            "gc_evicted_instruments",
            dry_run=dry_run,
            count=len(report.evicted),
            rows_deleted=report.rows_deleted,
        )
    return report


def _evict(session: Session, instrument_id: int) -> None:
    for model in (
        MarketPriceHistory,
        MarketCorporateAction,
        MarketPriceJob,
        MarketInstrumentPin,
        MarketPriceCoverage,
    ):
        session.execute(delete(model).where(model.instrument_id == instrument_id))
    session.execute(delete(MarketInstrument).where(MarketInstrument.id == instrument_id))
    session.expire_all()
