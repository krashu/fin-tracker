"""Persistent job queue.

Jobs survive restarts, are idempotent per instrument (partial unique index on
queued/running rows) and are drained in priority order by one worker.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import date, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, Select, delete, func, select, update
from sqlalchemy.orm import Session

from app.models.market_pricing import (
    ACTIVE_JOB_STATES,
    JOB_BACKFILL,
    JOB_FAILED,
    JOB_QUEUED,
    JOB_REFRESH,
    JOB_RUNNING,
    MarketInstrument,
    MarketPriceJob,
)

from .util import utcnow

PRIORITY_BACKGROUND = 0  # daily refresh, gap fills
PRIORITY_IMPORT = 50  # statement import; +1..49 by holding weight
PRIORITY_INTERACTIVE = 200  # a user is looking at this instrument right now


def active_job(session: Session, instrument_id: int) -> MarketPriceJob | None:
    return session.scalars(
        select(MarketPriceJob).where(
            MarketPriceJob.instrument_id == instrument_id,
            MarketPriceJob.status.in_(ACTIVE_JOB_STATES),
        )
    ).first()


def enqueue(
    session: Session,
    instrument: MarketInstrument,
    *,
    kind: str = JOB_BACKFILL,
    priority: int = PRIORITY_BACKGROUND,
    range_start: date | None = None,
    now: datetime | None = None,
) -> MarketPriceJob:
    """Queue a fetch, or strengthen the job that is already queued."""
    now = now or utcnow()
    existing = active_job(session, instrument.id)
    if existing is not None:
        existing.priority = max(existing.priority, priority)
        if existing.status == JOB_QUEUED:
            if kind == JOB_BACKFILL and existing.kind != JOB_BACKFILL:
                existing.kind, existing.range_start = JOB_BACKFILL, None
            elif kind == JOB_REFRESH and existing.kind == JOB_REFRESH and range_start:
                existing.range_start = min(existing.range_start or range_start, range_start)
        existing.updated_at = now
        return existing

    session.execute(
        delete(MarketPriceJob).where(
            MarketPriceJob.instrument_id == instrument.id, MarketPriceJob.status == JOB_FAILED
        )
    )
    job = MarketPriceJob(
        instrument_id=instrument.id,
        kind=kind,
        host=instrument.source,
        priority=priority,
        status=JOB_QUEUED,
        range_start=range_start if kind == JOB_REFRESH else None,
        not_before=now,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.flush()
    return job


def bump_priority(
    session: Session, instrument_id: int, priority: int = PRIORITY_INTERACTIVE
) -> bool:
    """Move an instrument's pending job to the front. True if a job was waiting."""
    job = active_job(session, instrument_id)
    if job is None:
        return False
    if job.priority < priority:
        job.priority = priority
    return True


def _runnable(now: datetime, paused_hosts: Collection[str]) -> Select[MarketPriceJob]:
    stmt = select(MarketPriceJob).where(
        MarketPriceJob.status == JOB_QUEUED, MarketPriceJob.not_before <= now
    )
    if paused_hosts:
        stmt = stmt.where(MarketPriceJob.host.not_in(list(paused_hosts)))
    return stmt


def runnable_heads(
    session: Session, *, now: datetime, paused_hosts: Collection[str] = ()
) -> list[MarketPriceJob]:
    """The best runnable job of each host that is not paused (nothing is claimed)."""
    hosts = session.scalars(
        select(MarketPriceJob.host)
        .where(MarketPriceJob.status == JOB_QUEUED, MarketPriceJob.not_before <= now)
        .distinct()
    ).all()
    heads = []
    for host in hosts:
        if host in paused_hosts:
            continue
        head = session.scalars(
            _runnable(now, ())
            .where(MarketPriceJob.host == host)
            .order_by(MarketPriceJob.priority.desc(), MarketPriceJob.id)
            .limit(1)
        ).first()
        if head is not None:
            heads.append(head)
    return heads


def mark_running(job: MarketPriceJob, now: datetime) -> MarketPriceJob:
    job.status = JOB_RUNNING
    job.updated_at = now
    return job


def claim_next(
    session: Session, *, now: datetime, paused_hosts: Collection[str] = ()
) -> MarketPriceJob | None:
    """Take the highest-priority runnable job and mark it running."""
    stmt = (
        _runnable(now, paused_hosts)
        .order_by(MarketPriceJob.priority.desc(), MarketPriceJob.id)
        .limit(1)
    )
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    job = session.scalars(stmt).first()
    return mark_running(job, now) if job is not None else None


def complete(session: Session, job_id: int) -> None:
    """Finished jobs are deleted; the coverage row is the durable record."""
    session.execute(delete(MarketPriceJob).where(MarketPriceJob.id == job_id))


def release(
    session: Session,
    job_id: int,
    *,
    not_before: datetime,
    error: str,
    count_attempt: bool,
) -> MarketPriceJob | None:
    """Put a running job back in the queue for a later try."""
    job = session.get(MarketPriceJob, job_id)
    if job is None:
        return None
    job.status = JOB_QUEUED
    job.not_before = not_before
    job.last_error = error[:500]
    if count_attempt:
        job.attempts += 1
    job.updated_at = utcnow()
    return job


def fail(session: Session, job_id: int, error: str) -> None:
    job = session.get(MarketPriceJob, job_id)
    if job is not None:
        job.status = JOB_FAILED
        job.last_error = error[:500]
        job.updated_at = utcnow()


def recover_interrupted(session: Session) -> int:
    """Re-queue jobs left 'running' by a crash or power cut. Call on worker start."""
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(MarketPriceJob)
            .where(MarketPriceJob.status == JOB_RUNNING)
            .values(status=JOB_QUEUED)
        ),
    )
    return result.rowcount or 0


def retry_failed(session: Session, now: datetime | None = None) -> int:
    """Give every failed job another full set of attempts."""
    now = now or utcnow()
    count = 0
    for job in session.scalars(
        select(MarketPriceJob).where(MarketPriceJob.status == JOB_FAILED)
    ).all():
        if active_job(session, job.instrument_id) is not None:
            session.delete(job)
            continue
        job.status, job.attempts, job.not_before, job.updated_at = (
            JOB_QUEUED,
            0,
            now,
            now,
        )
        session.flush()
        count += 1
    return count


def backoff_delay(attempts: int, base_s: float, max_s: float) -> timedelta:
    return timedelta(seconds=min(max_s, base_s * (2 ** max(0, attempts))))


def queue_stats(session: Session) -> dict[str, dict[str, int]]:
    """``{host: {status: count}}`` for the status endpoint."""
    rows = session.execute(
        select(MarketPriceJob.host, MarketPriceJob.status, func.count()).group_by(
            MarketPriceJob.host, MarketPriceJob.status
        )
    ).all()
    stats: dict[str, dict[str, int]] = {}
    for host, status, count in rows:
        stats.setdefault(host, {})[status] = count
    return stats
