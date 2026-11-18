"""PeerEntry model — individual ledger entries in the peer debt auxiliary ledger (ADR-0017).

Amounts are positive BigInteger in paise. Direction indicates whether the user fronted/lent money
('you_gave', receivable) or the contact fronted/lent to user ('you_got', payable).
"""

from __future__ import annotations

import uuid
from datetime import date as date_t
from typing import Literal, get_args

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    ForeignKeyConstraint,
    Index,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import expression

from app.models.base import Base, TimestampMixin

PeerDirectionStr = Literal["you_gave", "you_got"]


class PeerEntry(Base, TimestampMixin):
    __tablename__ = "peer_entries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["contact_id", "user_id"],
            ["peer_contacts.id", "peer_contacts.user_id"],
            name="fk_peer_entries_contact_id_peer_contacts",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["transaction_id", "user_id"],
            ["transactions.id", "transactions.user_id"],
            name="fk_peer_entries_transaction_id_transactions",
            ondelete="NO ACTION",
        ),
        Index("ix_peer_entries_user_contact", "user_id", "contact_id"),
        Index("ix_peer_entries_user_txn", "user_id", "transaction_id"),
        Index("ix_peer_entries_user_date", "user_id", "date"),
        CheckConstraint("amount_paise > 0", name="amount_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column()
    contact_id: Mapped[int] = mapped_column()
    transaction_id: Mapped[int | None] = mapped_column(default=None)
    date: Mapped[date_t] = mapped_column(Date)
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    direction: Mapped[PeerDirectionStr] = mapped_column(
        Enum(
            *get_args(PeerDirectionStr),
            name="peer_direction",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        )
    )
    is_settlement: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=expression.false()
    )
    notes: Mapped[str | None] = mapped_column(Text, default=None)
