"""Investment account model — demat, mutual fund folio, US brokerage, manual assets.

ADR-0019: Investment accounts partition investments across brokers/depositories.
FIFO lot matching (ADR-0015, CBDT Circular No. 768) is scoped per (account_id, instrument_id).
Composite unique constraints enforce tenant isolation (ADR-0003):
- uq_investment_accounts_id_user: (id, user_id) target for composite FKs
- uq_investment_accounts_user_provider_ref: (user_id, provider, external_account_ref)
"""

from __future__ import annotations

import uuid
from typing import Literal, get_args

from sqlalchemy import Boolean, Enum, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import expression

from app.models.account import CurrencyStr
from app.models.base import Base, TimestampMixin

InvestmentAccountTypeStr = Literal["demat", "mf_folio", "us_broker", "bank_fd", "nps", "manual"]


class InvestmentAccount(Base, TimestampMixin):
    __tablename__ = "investment_accounts"
    __table_args__ = (
        Index("uq_investment_accounts_id_user", "id", "user_id", unique=True),
        UniqueConstraint(
            "user_id",
            "provider",
            "external_account_ref",
            name="uq_investment_accounts_user_provider_ref",
        ),
        Index("ix_investment_accounts_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    account_type: Mapped[InvestmentAccountTypeStr] = mapped_column(
        Enum(
            *get_args(InvestmentAccountTypeStr),
            name="investment_account_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    external_account_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    currency: Mapped[CurrencyStr] = mapped_column(
        Enum(
            *get_args(CurrencyStr),
            name="investment_account_currency",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
        default="INR",
        server_default="INR",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=expression.true()
    )
