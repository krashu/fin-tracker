"""FX rate cache (PRD §F7 / §Data model) — daily INR↔USD snapshots.

A ``fx_rates`` row is one currency-pair rate on one date — **global reference data,
no ``user_id``** (the rate for a date is the same for every user), like ``benchmarks``.
Backfilled from a free provider (``frankfurter.app``) by ``fx_service.refresh_fx_rates``
and *read* on the holdings / portfolio / ingest paths — never fetched there.

Used for: (a) stamping ``investment_transactions.fx_rate_to_inr`` at the transaction date
(historical clock), and (b) converting a USD ``current_nav`` to INR for portfolio rollups
(as-of clock). Reads carry-forward over weekends/holidays (last-known rate ``date <= on``);
the cache is never synthesised for non-trading days.

The model class is named ``FxRateQuote`` to avoid colliding with the ``FxRate`` scaled-decimal
*type* (``models/types.py``); ``rate`` reuses that 6dp type. ``from_currency`` / ``to_currency``
each declare their OWN enum name (``Enum(native_enum=False)`` — VARCHAR + CHECK, no native
ENUM) for the same SQLite→Postgres portability reason as the other tables. They must NOT
share one name: the ``ck_%(table_name)s_%(constraint_name)s`` convention would then emit
two CHECKs called ``ck_fx_rates_currency`` in one table, which Postgres rejects outright
(42710) while SQLite silently tolerates it.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import get_args

from sqlalchemy import CheckConstraint, Date, Enum, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.account import CurrencyStr
from app.models.base import Base, TimestampMixin
from app.models.types import FxRate


class FxRateQuote(Base, TimestampMixin):
    __tablename__ = "fx_rates"
    __table_args__ = (
        # One rate per (pair, date). Named explicitly (not an auto-named UniqueConstraint,
        # whose convention would drop to/date from the name) and shaped as the carry-forward
        # read index (WHERE from=? AND to=? AND date<=? ORDER BY date DESC) — mirrors
        # benchmark_nav's uq_benchmark_nav_benchmark_id_nav_date.
        Index(
            "uq_fx_rates_from_currency_to_currency_date",
            "from_currency",
            "to_currency",
            "date",
            unique=True,
        ),
        # rate is a scaled int (×1e6); a positive FX rate scales to a positive int, so this
        # CHECK on the stored bigint guards against a poisoned 0 / negative seed corrupting
        # every conversion. test_migration_parity compares its NAME and clause text against the
        # migration's, so renaming it here without renaming it in 0015 fails the gate.
        CheckConstraint("rate > 0", name="rate_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # ``datetime.date``, not a bare ``date`` import: the column attribute below is itself named
    # ``date``, so a bare name would be shadowed by the attribute inside its own annotation.
    date: Mapped[datetime.date] = mapped_column(Date)
    from_currency: Mapped[CurrencyStr] = mapped_column(
        Enum(
            *get_args(CurrencyStr),
            name="from_currency",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        )
    )
    to_currency: Mapped[CurrencyStr] = mapped_column(
        Enum(
            *get_args(CurrencyStr),
            name="to_currency",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        )
    )
    rate: Mapped[Decimal] = mapped_column(FxRate())
    source: Mapped[str] = mapped_column(String(32))
