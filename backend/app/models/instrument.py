"""Instrument model — one row per scheme/ticker the user holds (PRD §F7).

Carries ``user_id`` but deliberately **no** ``account_id``: investments are a
separate world from the spend ``accounts``/``transactions`` tables (PRD §Data
model). ``(symbol, currency)`` is the dedup key — the broker ticker / scheme handle
(normalised) plus its currency; active (non-archived) ``(symbol, currency)`` pairs are
unique per user via the partial index, so the same ticker can exist once as an INR holding
and once as a USD holding (a cross-listed name) without colliding, and soft-delete +
re-create still works (mirrors the accounts / categories pattern). ``isin`` (12-char ISO
6166) and ``amfi_code`` are optional identity keys used to match a holding to its NAV/price
(AMFI NAVAll for MFs, equity quotes) — nullable, captured at import.

``current_nav`` is the latest price in native currency. **``nav_updated_at`` is the
VALUATION DATE — the date that price is effective for — on every write path**, with no
polymorphic escape hatch: the auto snapshot (AMFI / Yahoo, PRD §F7/§F9) writes the
source's NAV date, and the manual POST / PATCH writes the client's ``nav_as_of``
(defaulting to today). This module owns that sentence; every other site restates it by
reference. It matters because all three readers — the holdings age, the portfolio
staleness flag, and ``_source_is_newer``'s never-regress guard — subtract it from a date
and present the result as "how old is this valuation". A write-time stamp made those
readers lie about exactly the hand-priced holdings (``fd`` / ``bond`` / ``nps`` /
``gold`` / ``other``) that no refresh can ever correct.

It is stored **naive UTC** whichever path writes it (ADR-0001 rule 5): an aware bind is
assignment-cast through the Postgres server's ``TimeZone``, which on a negative-offset
server moves a midnight-UTC valuation stamp onto the previous day. Both nullable — a
fresh instrument may have no NAV yet, in which case the holdings read-model reports
current value as unavailable, and clearing the NAV clears its valuation date with it.

``currency`` reuses the accounts enum; the investment side supports INR + USD and the
currency is part of the active-symbol identity above (so a cross-listed ticker can be held
in both). ``asset_class`` and ``exchange`` use ``Enum(native_enum=False)`` for the same
SQLite→Postgres portability reason as the spend tables.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal, get_args

from sqlalchemy import Enum, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.account import CurrencyStr
from app.models.base import Base, TimestampMixin
from app.models.types import PriceNative

AssetClassStr = Literal[
    "indian_equity",
    "indian_mf",
    "us_equity",
    "us_etf",
    "fd",
    "bond",
    "nps",
    "gold",
    "other",
]
ExchangeStr = Literal["NSE", "BSE", "MFCentral", "NASDAQ", "NYSE", "OTHER"]


class Instrument(Base, TimestampMixin):
    __tablename__ = "instruments"
    __table_args__ = (
        Index(
            "uq_instruments_active_user_symbol_currency",
            "user_id",
            "symbol",
            "currency",
            unique=True,
            sqlite_where=text("archived_at IS NULL"),
            postgresql_where=text("archived_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    symbol: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(256))
    asset_class: Mapped[AssetClassStr] = mapped_column(
        Enum(
            *get_args(AssetClassStr),
            name="asset_class",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        )
    )
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
    exchange: Mapped[ExchangeStr] = mapped_column(
        Enum(
            *get_args(ExchangeStr),
            name="exchange",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        )
    )
    current_nav: Mapped[Decimal | None] = mapped_column(PriceNative(), nullable=True)
    nav_updated_at: Mapped[datetime | None] = mapped_column(default=None)
    archived_at: Mapped[datetime | None] = mapped_column(default=None)
    isin: Mapped[str | None] = mapped_column(String(12), nullable=True)
    amfi_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
