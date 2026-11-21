"""Allocation target model — portfolio target allocation weights and rebalancing thresholds.

Milestone 4: Target Allocation & Intelligent Rebalancing (ADR-0003, ADR-0019).
Supports portfolio-wide targets (scope_type="all", account_id=None) and
account-specific targets (scope_type="account", account_id=<id>).
Enforces tenant isolation via composite same-user FK (account_id, user_id).
"""

from __future__ import annotations

import uuid
from typing import Literal, get_args

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

AllocationScopeTypeStr = Literal["all", "account"]
AllocationTargetTypeStr = Literal["asset_class", "instrument", "tag"]


class AllocationTarget(Base, TimestampMixin):
    __tablename__ = "allocation_targets"
    __table_args__ = (
        Index("uq_allocation_targets_id_user", "id", "user_id", unique=True),
        Index("ix_allocation_targets_user_id", "user_id"),
        Index("ix_allocation_targets_user_scope", "user_id", "scope_type", "account_id"),
        ForeignKeyConstraint(
            ["account_id", "user_id"],
            ["investment_accounts.id", "investment_accounts.user_id"],
            name="fk_allocation_targets_account_id_investment_accounts",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "user_id",
            "scope_type",
            "account_id",
            "target_type",
            "target_key",
            name="uq_allocation_targets_scope_target",
        ),
        Index(
            "uq_allocation_targets_all_scope",
            "user_id",
            "scope_type",
            "target_type",
            "target_key",
            unique=True,
            sqlite_where=text("account_id IS NULL"),
            postgresql_where=text("account_id IS NULL"),
        ),
        CheckConstraint(
            "(scope_type = 'all' AND account_id IS NULL) "
            "OR (scope_type = 'account' AND account_id IS NOT NULL)",
            name="scope_account_consistency",
        ),
        CheckConstraint("target_bps >= 0 AND target_bps <= 10000", name="target_bps_range"),
        CheckConstraint(
            "rebalance_threshold_bps >= 0 AND rebalance_threshold_bps <= 10000",
            name="rebalance_threshold_bps_range",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    scope_type: Mapped[AllocationScopeTypeStr] = mapped_column(
        Enum(
            *get_args(AllocationScopeTypeStr),
            name="allocation_scope_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
        default="all",
        server_default="all",
    )
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_type: Mapped[AllocationTargetTypeStr] = mapped_column(
        Enum(
            *get_args(AllocationTargetTypeStr),
            name="allocation_target_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
    )
    target_key: Mapped[str] = mapped_column(String(64), nullable=False)
    target_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    rebalance_threshold_bps: Mapped[int] = mapped_column(
        Integer, nullable=False, default=500, server_default=text("500")
    )
