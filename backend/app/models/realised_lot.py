"""Realised lot — the part of one lot a sell consumed (ADR-0015 rule 5).

One row per ``(sell, lot)`` pair: when a ``sell`` or ``switch_out`` consumes (part of) the
lot opened by an acquisition row, that match is recorded here with the units and cost
released, the sell's proportional share of its gross consideration and fees, and both
dates (the holding period runs ``buy_date`` → ``sell_date``). A same-day buy a sell netted
against (rule 7) is recorded the same way with ``kind`` ``intraday``; a consumed lot is
``fifo``. F11's capital-gains statement reads only ``fifo`` rows (rule 5), and the intraday
listing reads the ``intraday`` ones (rule 8). "buy" in ``buy_txn_id``
means any acquisition: ``buy``, ``sip``, ``switch_in``, ``bonus`` or the reinvestment
``buy`` leg.

The rows are **derived**. The only writer is
:func:`app.services.holdings_service.rebuild_realised_lots`, which replays one
``(user, instrument)`` through the same FIFO code holdings use and replaces that
instrument's rows, inside the same DB transaction as every investment write. Holdings never
read this table; F11 does. Units, cost, proceeds and fees are all native (the instrument's
currency); INR conversion belongs to the reader (ADR-0016, ADR-0014). Archived instruments
keep their rows. The table is not part of the backup: it is rebuilt from
``investment_transactions``.

``user_id`` has no direct ``users`` FK: integrity rides on the two composite same-user FKs
(ADR-0003 rule 10), as for ``transaction_label``. Both are ``ON DELETE CASCADE``, so
deleting an investment row never trips an FK on derived data; the route then rebuilds.

``proceeds_native_paise`` / ``sell_fees_native_paise`` are this lot's shares of the sell's
``amount_native_paise`` / ``fees_native_paise``, allocated in replay order as the half-even
proportional share of what remains by units, the last lot taking the remainder, so the shares
sum exactly to the sell's totals.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Literal, get_args

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Enum,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.types import Units

# ``fifo``: a lot a sell consumed (a capital gain). ``intraday``: a same-day buy a sell
# netted against (ADR-0015 rule 7), reported apart from capital gains (rule 8).
RealisedLotKindStr = Literal["fifo", "intraday"]


class RealisedLot(Base, TimestampMixin):
    __tablename__ = "realised_lots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "user_id"],
            ["investment_accounts.id", "investment_accounts.user_id"],
            name="fk_realised_lots_account_id_investment_accounts",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["sell_txn_id", "user_id"],
            ["investment_transactions.id", "investment_transactions.user_id"],
            name="fk_realised_lots_sell_txn_id_investment_transactions",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["buy_txn_id", "user_id"],
            ["investment_transactions.id", "investment_transactions.user_id"],
            name="fk_realised_lots_buy_txn_id_investment_transactions",
            ondelete="CASCADE",
        ),
        # One row per (sell, lot); also the child-side index for the sell FK.
        UniqueConstraint(
            "user_id", "sell_txn_id", "buy_txn_id", name="uq_realised_lots_user_sell_buy"
        ),
        # Child-side index for the buy FK cascade.
        Index("ix_realised_lots_user_buy", "user_id", "buy_txn_id"),
        CheckConstraint("units > 0", name="positive_units"),
        CheckConstraint("cost_native_paise >= 0", name="non_negative_cost"),
        CheckConstraint("proceeds_native_paise >= 0", name="non_negative_proceeds"),
        CheckConstraint("sell_fees_native_paise >= 0", name="non_negative_sell_fees"),
        CheckConstraint("buy_date <= sell_date", name="buy_not_after_sell"),
        CheckConstraint("kind = 'fifo' OR buy_date = sell_date", name="intraday_same_day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # NOT NULL: a nullable member would slacken a composite FK to MATCH-SIMPLE.
    user_id: Mapped[uuid.UUID] = mapped_column()
    account_id: Mapped[int] = mapped_column()
    sell_txn_id: Mapped[int] = mapped_column()
    buy_txn_id: Mapped[int] = mapped_column()
    units: Mapped[Decimal] = mapped_column(Units())
    cost_native_paise: Mapped[int] = mapped_column(BigInteger)
    proceeds_native_paise: Mapped[int] = mapped_column(BigInteger)
    sell_fees_native_paise: Mapped[int] = mapped_column(BigInteger)
    buy_date: Mapped[date] = mapped_column()
    sell_date: Mapped[date] = mapped_column()
    # No default, ORM or server: the kind is tax-relevant, so a writer that forgets it must fail
    # NOT NULL rather than file an intraday match as a capital gain (as fx_rate_to_inr, ADR-0016).
    kind: Mapped[RealisedLotKindStr] = mapped_column(
        Enum(
            *get_args(RealisedLotKindStr),
            name="realised_lot_kind",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        )
    )
