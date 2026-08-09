"""Benchmark reference data + NAV cache (PRD §F8 view 5, §Data model).

A ``benchmarks`` row is a curated INR index *fund* (TRI growth NAV, post-expense)
the user measures their portfolio against — **global reference data, no ``user_id``**
(every user compares against the same catalog, seeded by migration 0014).
``benchmark_nav`` is that fund's daily NAV history, backfilled from mfapi at seed
time (``benchmark_service.refresh_benchmark_navs``) and *read* on the
``GET /portfolio/performance`` path — never fetched there.

* ``amfi_code`` — the AMFI scheme code (mfapi keys on the same code). It points at the
  **growth** scheme so the replay prices off total-return NAV, not a div-payout series.
* ``inception_date`` — optional display metadata. The *effective* pricing inception the
  counterfactual clamps to is the earliest cached ``benchmark_nav.nav_date``
  (authoritative once backfilled), not this column.
* ``archived_at`` — house soft-delete idiom (``GET /benchmarks`` filters ``IS NULL``).
  v1 ships no benchmarks CRUD, so nothing writes it yet — present for §Data-model
  fidelity + the list filter (foreseeable delete), per the long-term-over-surgical call.

``kind`` / ``currency`` use ``Enum(native_enum=False)`` for the same SQLite→Postgres
portability reason as the spend/instrument tables (VARCHAR + CHECK, no native ENUM).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, get_args

from sqlalchemy import Date, Enum, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.account import CurrencyStr
from app.models.base import Base, TimestampMixin
from app.models.types import PriceNative

BenchmarkKindStr = Literal["index_fund"]


class Benchmark(Base, TimestampMixin):
    __tablename__ = "benchmarks"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[BenchmarkKindStr] = mapped_column(
        Enum(
            *get_args(BenchmarkKindStr),
            name="benchmark_kind",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        default="index_fund",
        server_default="index_fund",
    )
    amfi_code: Mapped[str] = mapped_column(String(16))
    currency: Mapped[CurrencyStr] = mapped_column(
        Enum(
            *get_args(CurrencyStr),
            name="currency",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        default="INR",
        server_default="INR",
    )
    inception_date: Mapped[date | None] = mapped_column(Date, default=None)
    archived_at: Mapped[datetime | None] = mapped_column(default=None)


class BenchmarkNav(Base, TimestampMixin):
    __tablename__ = "benchmark_nav"
    __table_args__ = (
        # Dedup + the forward-pricing lookup index (WHERE benchmark_id=? ORDER BY
        # nav_date). Named explicitly (not an auto-named UniqueConstraint, whose
        # convention would drop nav_date from the name) — mirrors 0007's
        # uq_investment_transactions_id_user shape.
        Index(
            "uq_benchmark_nav_benchmark_id_nav_date",
            "benchmark_id",
            "nav_date",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    benchmark_id: Mapped[int] = mapped_column(ForeignKey("benchmarks.id"))
    nav_date: Mapped[date] = mapped_column(Date)
    nav: Mapped[Decimal] = mapped_column(PriceNative())
