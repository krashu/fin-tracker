"""add allocation_targets table

Revision ID: 0054_allocation_targets
Revises: 0053_portfolio_daily_snapshots
Create Date: 2026-11-21

Milestone 4 Phase 1:
1. Creates table ``allocation_targets`` scoped to user_id:
   - Tracks portfolio-wide or account-scoped targets in basis points (10000 = 100%)
   - Scope types: 'all', 'account'
   - Target types: 'asset_class', 'instrument', 'tag'
   - Composite same-user FK (account_id, user_id) -> (investment_accounts.id, user_id)
   - Unique constraints for scoped and global targets
   - Range and consistency check constraints
2. Foreign key to users.id with CASCADE deletion.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0054_allocation_targets"
down_revision: str | Sequence[str] | None = "0053_portfolio_daily_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCOPE_TYPE_ENUM = sa.Enum(
    "all",
    "account",
    name="allocation_scope_type",
    native_enum=False,
    create_constraint=True,
    validate_strings=True,
)

_TARGET_TYPE_ENUM = sa.Enum(
    "asset_class",
    "instrument",
    "tag",
    name="allocation_target_type",
    native_enum=False,
    create_constraint=True,
    validate_strings=True,
)


def upgrade() -> None:
    op.create_table(
        "allocation_targets",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("scope_type", _SCOPE_TYPE_ENUM, nullable=False, server_default="all"),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("target_type", _TARGET_TYPE_ENUM, nullable=False),
        sa.Column("target_key", sa.String(64), nullable=False),
        sa.Column("target_bps", sa.Integer(), nullable=False),
        sa.Column(
            "rebalance_threshold_bps",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("500"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_allocation_targets_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "user_id"],
            ["investment_accounts.id", "investment_accounts.user_id"],
            name="fk_allocation_targets_account_id_investment_accounts",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "user_id",
            "scope_type",
            "account_id",
            "target_type",
            "target_key",
            name="uq_allocation_targets_scope_target",
        ),
        sa.CheckConstraint(
            "(scope_type = 'all' AND account_id IS NULL) "
            "OR (scope_type = 'account' AND account_id IS NOT NULL)",
            name="scope_account_consistency",
        ),
        sa.CheckConstraint("target_bps >= 0 AND target_bps <= 10000", name="target_bps_range"),
        sa.CheckConstraint(
            "rebalance_threshold_bps >= 0 AND rebalance_threshold_bps <= 10000",
            name="rebalance_threshold_bps_range",
        ),
    )
    op.create_index(
        "uq_allocation_targets_id_user",
        "allocation_targets",
        ["id", "user_id"],
        unique=True,
    )
    op.create_index(
        "ix_allocation_targets_user_id",
        "allocation_targets",
        ["user_id"],
    )
    op.create_index(
        "ix_allocation_targets_user_scope",
        "allocation_targets",
        ["user_id", "scope_type", "account_id"],
    )
    op.create_index(
        "uq_allocation_targets_all_scope",
        "allocation_targets",
        ["user_id", "scope_type", "target_type", "target_key"],
        unique=True,
        sqlite_where=sa.text("account_id IS NULL"),
        postgresql_where=sa.text("account_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_allocation_targets_all_scope", table_name="allocation_targets")
    op.drop_index("ix_allocation_targets_user_scope", table_name="allocation_targets")
    op.drop_index("ix_allocation_targets_user_id", table_name="allocation_targets")
    op.drop_index("uq_allocation_targets_id_user", table_name="allocation_targets")
    op.drop_table("allocation_targets")
