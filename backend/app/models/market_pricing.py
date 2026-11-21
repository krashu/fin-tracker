"""Market pricing models (ADR-0021, PRD §F7/§F8/§F9).

Clustered, normalized market pricing store supporting global-by-design caching,
split adjustments, and job tracking.
"""

from __future__ import annotations

from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, utcnow_default
from app.models.types import CompactDate

# Coverage states
COVERAGE_PENDING = "pending"  # never fetched yet
COVERAGE_OK = "ok"  # has data
COVERAGE_ERROR = "error"  # last attempts failed, will be retried
COVERAGE_NOT_FOUND = "not_found"  # upstream never knew this symbol
COVERAGE_DEAD = "dead"  # had data, upstream stopped serving it

# Job states
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_FAILED = "failed"
ACTIVE_JOB_STATES = (JOB_QUEUED, JOB_RUNNING)

JOB_BACKFILL = "backfill"  # fetch everything upstream has
JOB_REFRESH = "refresh"  # fetch from range_start to today


class MarketInstrument(Base):
    __tablename__ = "market_instruments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # ISIN or EXCHANGE:SYMBOL, exactly as the rest of fin-tracker refers to it.
    price_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    asset_class: Mapped[str] = mapped_column(String(12), nullable=False)  # mf|equity|fx|index
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)  # provider / host key
    provider_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Upstream no longer serves this symbol; cached history is irreplaceable.
    upstream_dead: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa.false()
    )
    # Set by GC when nothing references the instrument; eviction waits out a grace period.
    unreferenced_since: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow_default)

    coverage: Mapped[MarketPriceCoverage] = relationship(
        back_populates="instrument", uselist=False, cascade="all, delete-orphan"
    )


class MarketPriceHistory(Base):
    """Raw (as-traded, unadjusted) daily closes / NAVs. Sparse: trading days only."""

    __tablename__ = "market_price_history"
    # The composite PK *is* the table: no rowid, no second copy of the key in an index.
    __table_args__ = (
        PrimaryKeyConstraint("instrument_id", "nav_date", name="pk_market_price_history"),
        {"sqlite_with_rowid": False},
    )

    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("market_instruments.id", ondelete="CASCADE"), primary_key=True
    )
    nav_date: Mapped[date] = mapped_column(CompactDate, primary_key=True)
    nav_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)  # 10^-4 currency units


class MarketPriceCoverage(Base):
    __tablename__ = "market_price_coverage"

    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("market_instruments.id", ondelete="CASCADE"), primary_key=True
    )
    covered_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    covered_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, default=COVERAGE_PENDING, server_default=COVERAGE_PENDING
    )
    # True once an unbounded fetch succeeded: upstream has nothing before covered_from.
    inception_reached: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa.false()
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    instrument: Mapped[MarketInstrument] = relationship(back_populates="coverage")


class MarketCorporateAction(Base):
    __tablename__ = "market_corporate_actions"
    __table_args__ = (
        UniqueConstraint("instrument_id", "action_date", "kind", name="uq_market_corp_action"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("market_instruments.id", ondelete="CASCADE"), nullable=False
    )
    action_date: Mapped[date] = mapped_column(Date, nullable=False)  # ex-date
    kind: Mapped[str] = mapped_column(
        String(12), nullable=False, default="split", server_default="split"
    )
    # A 10-for-1 split is numerator=10, denominator=1. Yahoo occasionally sends
    # fractional components, hence floats.
    numerator: Mapped[float] = mapped_column(Float, nullable=False)
    denominator: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)


class MarketInstrumentPin(Base):
    __tablename__ = "market_instrument_pins"

    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("market_instruments.id", ondelete="CASCADE"), primary_key=True
    )
    reason: Mapped[str] = mapped_column(String(16), primary_key=True)  # benchmark|fx|manual
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow_default)


class MarketPriceJob(Base):
    __tablename__ = "market_price_jobs"
    __table_args__ = (
        # At most one queued/running job per instrument: enqueueing is idempotent.
        Index(
            "uq_market_price_jobs_active",
            "instrument_id",
            unique=True,
            sqlite_where=text("status IN ('queued', 'running')"),
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index("ix_market_price_jobs_claim", "status", "priority", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("market_instruments.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(
        String(12), nullable=False, default=JOB_BACKFILL, server_default=JOB_BACKFILL
    )
    host: Mapped[str] = mapped_column(String(24), nullable=False)
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, default=JOB_QUEUED, server_default=JOB_QUEUED
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    not_before: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow_default)
    range_start: Mapped[date | None] = mapped_column(Date, nullable=True)  # refresh jobs only
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow_default)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow_default)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class MarketHostState(Base):
    __tablename__ = "market_host_state"

    host: Mapped[str] = mapped_column(String(24), primary_key=True)
    paused_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    trips: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )  # consecutive
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow_default)


class MarketTaskRun(Base):
    __tablename__ = "market_task_runs"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_status: Mapped[str] = mapped_column(
        String(12), nullable=False, default="ok", server_default="ok"
    )
    last_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
