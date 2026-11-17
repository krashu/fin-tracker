"""F11 statement reads over the persisted lot match (ADR-0015 rules 4-5, 8).

Never replays, never writes, never commits: ``realised_lots`` is the source of truth,
rebuilt by :func:`app.services.holdings_service.rebuild_realised_lots` on every investment
write.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Instrument, InvestmentTransaction, RealisedLot


@dataclass(frozen=True, slots=True)
class IntradayTrade:
    sell_txn_id: int
    buy_txn_id: int
    instrument_id: int
    symbol: str
    name: str
    date: date
    units: Decimal
    buy_value_paise: int
    sell_value_paise: int
    sell_charges_paise: int
    profit_paise: int


@dataclass(frozen=True, slots=True)
class IntradayStatement:
    trades: list[IntradayTrade]
    total_profit_paise: int


def intraday_statement(
    session: Session, *, user_id: UUID, first: date, last: date
) -> IntradayStatement:
    """Intraday matches whose sell falls in ``first``..``last`` (inclusive), oldest first.

    Archived instruments are included: a past year's statement must not lose a trade
    because the instrument was archived since. Amounts are INR (``indian_equity`` is the
    only class that nets intraday, and it is INR at every write boundary).
    """
    rows = session.execute(
        select(RealisedLot, Instrument.id, Instrument.symbol, Instrument.name)
        .join(
            InvestmentTransaction,
            (InvestmentTransaction.id == RealisedLot.sell_txn_id)
            & (InvestmentTransaction.user_id == RealisedLot.user_id),
        )
        .join(
            Instrument,
            (Instrument.id == InvestmentTransaction.instrument_id)
            & (Instrument.user_id == RealisedLot.user_id),
        )
        .where(
            RealisedLot.user_id == user_id,
            RealisedLot.kind == "intraday",
            RealisedLot.sell_date >= first,
            RealisedLot.sell_date <= last,
        )
        .order_by(RealisedLot.sell_date, Instrument.symbol, RealisedLot.id)
    ).all()
    trades = [
        IntradayTrade(
            sell_txn_id=lot.sell_txn_id,
            buy_txn_id=lot.buy_txn_id,
            instrument_id=inst_id,
            symbol=symbol,
            name=name,
            date=lot.sell_date,
            units=lot.units,
            buy_value_paise=lot.cost_native_paise,
            sell_value_paise=lot.proceeds_native_paise,
            sell_charges_paise=lot.sell_fees_native_paise,
            profit_paise=lot.proceeds_native_paise
            - lot.sell_fees_native_paise
            - lot.cost_native_paise,
        )
        for lot, inst_id, symbol, name in rows
    ]
    return IntradayStatement(trades=trades, total_profit_paise=sum(t.profit_paise for t in trades))


def realised_sale_span(session: Session, *, user_id: UUID) -> tuple[date | None, date | None]:
    """Earliest and latest sell date over the user's realised lots (both kinds)."""
    lo, hi = session.execute(
        select(func.min(RealisedLot.sell_date), func.max(RealisedLot.sell_date)).where(
            RealisedLot.user_id == user_id
        )
    ).one()
    return lo, hi
