"""F11 statement reads over ``realised_lots`` (ADR-0015 rule 8; PRD §F11).

Drives :func:`app.services.tax_statement_service.intraday_statement` against an in-memory
session after a real :func:`rebuild_realised_lots`, so the listing is checked against the
persisted match rather than hand-built lot rows.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.core.clock import naive_utcnow
from app.core.fiscal_year import fy_bounds
from app.models import Instrument, InvestmentTransaction, InvestmentTxnTypeStr, User
from app.services.holdings_service import rebuild_realised_lots
from app.services.tax_statement_service import intraday_statement, realised_sale_span

FY25 = fy_bounds(2025)


def _instrument(
    session: Session,
    user_id: UUID,
    *,
    symbol: str = "EQ",
    asset_class: str = "indian_equity",
) -> Instrument:
    inst = Instrument(
        user_id=user_id,
        symbol=symbol,
        name=f"{symbol} Ltd",
        asset_class=asset_class,
        currency="INR",
        exchange="NSE",
        current_nav=Decimal("100"),
    )
    session.add(inst)
    session.flush()
    return inst


def _txn(
    session: Session,
    user_id: UUID,
    instrument_id: int,
    txn_type: InvestmentTxnTypeStr,
    on: date,
    units: str,
    amount: int,
    fees: int = 0,
) -> InvestmentTransaction:
    txn = InvestmentTransaction(
        user_id=user_id,
        instrument_id=instrument_id,
        date=on,
        transaction_type=txn_type,
        units=Decimal(units),
        price_per_unit_native=None,
        amount_native_paise=amount,
        fees_native_paise=fees,
        fx_rate_to_inr=Decimal("1"),
    )
    session.add(txn)
    session.flush()
    return txn


def _same_day_pair(
    session: Session, user_id: UUID, inst: Instrument, on: date, profit_paise: int = 49_500
) -> None:
    """Buy and sell 10 units on ``on``; profit = sell - fees(500) - buy."""
    _txn(session, user_id, inst.id, "buy", on, "10", 100_000)
    _txn(session, user_id, inst.id, "sell", on, "10", 100_000 + profit_paise + 500, fees=500)
    rebuild_realised_lots(session, user_id=user_id, instrument_id=inst.id)


def _shared(session: Session, user: User) -> Instrument:
    """100 units bought 2 Mar, 50 bought 3 Mar, 80 sold 3 Mar: 50 net intraday, 30 FIFO."""
    inst = _instrument(session, user.id)
    _txn(session, user.id, inst.id, "buy", date(2026, 3, 2), "100", 1_000_000)
    _txn(session, user.id, inst.id, "buy", date(2026, 3, 3), "50", 600_000)
    _txn(session, user.id, inst.id, "sell", date(2026, 3, 3), "80", 1_040_000, fees=800)
    rebuild_realised_lots(session, user_id=user.id, instrument_id=inst.id)
    return inst


def test_intraday_trades_lists_each_same_day_match(session: Session, user: User) -> None:
    _shared(session, user)
    stmt = intraday_statement(session, user_id=user.id, first=FY25[0], last=FY25[1])
    assert len(stmt.trades) == 1
    t = stmt.trades[0]
    assert (t.units, t.buy_value_paise, t.sell_value_paise, t.sell_charges_paise) == (
        Decimal("50"),
        600_000,
        650_000,
        500,
    )
    assert t.profit_paise == 49_500
    assert t.date == date(2026, 3, 3)
    assert stmt.total_profit_paise == 49_500


def test_intraday_trades_are_scoped_to_the_tax_year(session: Session, user: User) -> None:
    inst = _instrument(session, user.id)
    _same_day_pair(session, user.id, inst, date(2026, 3, 31))
    _same_day_pair(session, user.id, inst, date(2026, 4, 1))
    stmt = intraday_statement(session, user_id=user.id, first=FY25[0], last=FY25[1])
    assert [t.date for t in stmt.trades] == [date(2026, 3, 31)]


def test_intraday_trades_include_an_archived_instrument(session: Session, user: User) -> None:
    inst = _instrument(session, user.id)
    _same_day_pair(session, user.id, inst, date(2026, 3, 3))
    inst.archived_at = naive_utcnow()
    session.flush()
    stmt = intraday_statement(session, user_id=user.id, first=FY25[0], last=FY25[1])
    assert len(stmt.trades) == 1


def test_intraday_trades_are_the_callers_only(session: Session, user: User) -> None:
    other = User(id=uuid4())
    session.add(other)
    session.flush()
    _same_day_pair(session, user.id, _instrument(session, user.id), date(2026, 3, 3))
    _same_day_pair(session, other.id, _instrument(session, other.id), date(2026, 3, 3))
    stmt = intraday_statement(session, user_id=user.id, first=FY25[0], last=FY25[1])
    assert len(stmt.trades) == 1


def test_intraday_total_sums_every_trade(session: Session, user: User) -> None:
    _same_day_pair(
        session, user.id, _instrument(session, user.id, symbol="AAA"), date(2026, 3, 3), 40_000
    )
    _same_day_pair(
        session, user.id, _instrument(session, user.id, symbol="BBB"), date(2026, 3, 4), 9_500
    )
    stmt = intraday_statement(session, user_id=user.id, first=FY25[0], last=FY25[1])
    assert [t.profit_paise for t in stmt.trades] == [40_000, 9_500]
    assert stmt.total_profit_paise == 49_500


def test_realised_sale_span_covers_both_kinds(session: Session, user: User) -> None:
    mf = _instrument(session, user.id, symbol="MF", asset_class="indian_mf")
    _txn(session, user.id, mf.id, "buy", date(2023, 5, 1), "10", 100_000)
    _txn(session, user.id, mf.id, "sell", date(2023, 6, 5), "10", 110_000)
    rebuild_realised_lots(session, user_id=user.id, instrument_id=mf.id)
    _same_day_pair(session, user.id, _instrument(session, user.id), date(2026, 3, 3))
    assert realised_sale_span(session, user_id=user.id) == (date(2023, 6, 5), date(2026, 3, 3))
