"""Portfolio daily snapshot model (PRD §F8/§F9, Milestone 2 Phase 4).

Daily portfolio valuation, net invested capital, unitized TWR index, and daily cash flows.
Scoped per user with multi-scope support ('all', 'account', 'member').
Clustered natural primary key (user_id, scope_type, scope_id, snapshot_date).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    PrimaryKeyConstraint,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow_default
from app.models.types import CompactDate


class PortfolioDailySnapshot(Base):
    __tablename__ = "portfolio_daily_snapshots"
    __table_args__ = (
        PrimaryKeyConstraint(
            "user_id",
            "scope_type",
            "scope_id",
            "snapshot_date",
            name="pk_portfolio_daily_snapshots",
        ),
        ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_portfolio_daily_snapshots_user_id_users",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "scope_type IN ('all', 'account', 'member')",
            name="scope_type_valid",
        ),
        {"sqlite_with_rowid": False},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    scope_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    scope_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, default=0, server_default=text("0")
    )
    snapshot_date: Mapped[date] = mapped_column(CompactDate, primary_key=True)
    portfolio_value_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    invested_capital_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    net_cashflow_paise: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    twr_index_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    benchmark_value_paise: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow_default, server_default=func.now()
    )
