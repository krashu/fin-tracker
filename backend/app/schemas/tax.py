"""Wire shapes for ``/api/v1/tax`` (PRD §F11)."""

from __future__ import annotations

from datetime import date as date_t

from pydantic import BaseModel, ConfigDict

from app.schemas.investments import DecimalStr


class IntradayTradeRead(BaseModel):
    """One intraday match (ADR-0015 rules 7-8): a same-day buy a sell netted against, as
    persisted in ``realised_lots`` (kind ``intraday``). Speculative business income, never
    a capital gain. INR paise: ``indian_*`` instruments are INR at every write boundary."""

    model_config = ConfigDict(from_attributes=True)

    sell_txn_id: int
    buy_txn_id: int
    instrument_id: int
    symbol: str
    name: str
    date: date_t
    units: DecimalStr
    buy_value_paise: int  # realised_lots.cost_native_paise (buy amount + buy charges)
    sell_value_paise: int  # realised_lots.proceeds_native_paise (gross)
    sell_charges_paise: int  # realised_lots.sell_fees_native_paise
    profit_paise: int  # sell_value - sell_charges - buy_value (negative = loss)


class IntradayTradesResponse(BaseModel):
    """Envelope for ``GET /tax/intraday-trades``. ``period`` echoes the FY start year
    ("2026" = Tax year 2026-27). ``total_profit_paise`` is the sum of ``profit_paise``."""

    period: str
    trades: list[IntradayTradeRead]
    total_profit_paise: int
