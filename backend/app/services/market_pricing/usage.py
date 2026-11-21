"""Which instruments the households actually reference.

The pricing engine does not own fin-tracker's transactions table, so it asks
through this small interface. "Referenced" means *any transaction ever*, not
"currently held": a fully sold position still needs its price history for
XIRR, TWR and net-worth charts.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models.instrument import Instrument
from app.models.investment_transaction import InvestmentTransaction

from .symbols import resolve_price_key
from .util import coerce_date


@dataclass(frozen=True, slots=True)
class InstrumentUsage:
    price_key: str
    first_trade_date: date  # earliest transaction in any household
    last_activity_date: date  # latest transaction in any household
    is_open: bool  # some household still holds units


class UsageSource(Protocol):
    def usages(self, session: Session) -> list[InstrumentUsage]: ...


class StaticUsageSource:
    """Fixed list; for tests, scripts and demos."""

    def __init__(self, usages: Iterable[InstrumentUsage] = ()) -> None:
        self._usages = list(usages)

    def set(self, usages: Iterable[InstrumentUsage]) -> None:
        self._usages = list(usages)

    def usages(self, session: Session) -> list[InstrumentUsage]:
        return list(self._usages)


class SqlUsageSource:
    """Derive usage from raw SQL."""

    def __init__(self, sql: str) -> None:
        self._sql = text(sql)

    def usages(self, session: Session) -> list[InstrumentUsage]:
        rows = session.execute(self._sql).mappings().all()
        return [
            InstrumentUsage(
                price_key=str(row["price_key"]),
                first_trade_date=coerce_date(row["first_trade_date"]),
                last_activity_date=coerce_date(row["last_activity_date"]),
                is_open=bool(row["is_open"]),
            )
            for row in rows
        ]


class FinTrackerUsageSource:
    """Derives instrument usage from fin-tracker's instruments and
    investment_transactions tables.
    """

    def usages(self, session: Session) -> list[InstrumentUsage]:
        # Query distinct instruments that have investment transactions
        stmt = (
            select(
                Instrument.isin,
                Instrument.amfi_code,
                Instrument.symbol,
                Instrument.exchange,
                Instrument.asset_class,
                func.min(InvestmentTransaction.date).label("first_trade_date"),
                func.max(InvestmentTransaction.date).label("last_trade_date"),
                func.sum(InvestmentTransaction.units).label("total_units"),
            )
            .join(
                InvestmentTransaction,
                InvestmentTransaction.instrument_id == Instrument.id,
            )
            .where(
                Instrument.archived_at.is_(None),
            )
            .group_by(
                Instrument.isin,
                Instrument.amfi_code,
                Instrument.symbol,
                Instrument.exchange,
                Instrument.asset_class,
            )
        )
        rows = session.execute(stmt).all()

        usages: dict[str, InstrumentUsage] = {}
        for r in rows:
            isin, amfi_code, symbol, exchange, asset_class, first_d, last_d, total_units = r
            if isin and isin.startswith("INF"):
                key = isin
            elif amfi_code and asset_class == "indian_mf":
                key = f"AMFI:{amfi_code}"
            elif exchange in ("NSE", "BSE", "NASDAQ", "NYSE"):
                key = f"{exchange}:{symbol}"
            else:
                key = symbol

            try:
                canonical = resolve_price_key(key).price_key
            except ValueError:
                continue

            first_date = coerce_date(first_d)
            last_date = coerce_date(last_d)
            is_open = bool(total_units and total_units > 0)

            if canonical in usages:
                existing = usages[canonical]
                usages[canonical] = InstrumentUsage(
                    price_key=canonical,
                    first_trade_date=min(existing.first_trade_date, first_date),
                    last_activity_date=max(existing.last_activity_date, last_date),
                    is_open=existing.is_open or is_open,
                )
            else:
                usages[canonical] = InstrumentUsage(
                    price_key=canonical,
                    first_trade_date=first_date,
                    last_activity_date=last_date,
                    is_open=is_open,
                )

        return list(usages.values())
