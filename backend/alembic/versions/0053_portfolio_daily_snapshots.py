"""add portfolio_daily_snapshots table

Revision ID: 0053_portfolio_daily_snapshots
Revises: 0052_market_pricing
Create Date: 2026-11-21

Milestone 2 Phase 4:
1. Creates table ``portfolio_daily_snapshots`` scoped to user_id:
   - Clustered natural PK (user_id, scope_type, scope_id, snapshot_date)
   - WITHOUT ROWID on SQLite
   - CompactDate on SQLite (integer), Date on Postgres
   - Scope types: 'all', 'account', 'member'
   - Net cashflows, unitized TWR index, benchmark value
2. Foreign key to users.id with CASCADE deletion.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0053_portfolio_daily_snapshots"
down_revision: str | Sequence[str] | None = "0052_market_pricing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    snapshot_date_type = sa.Integer() if is_sqlite else sa.Date()

    table_kwargs: dict[str, object] = {}
    if is_sqlite:
        table_kwargs["sqlite_with_rowid"] = False

    op.create_table(
        "portfolio_daily_snapshots",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("scope_id", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("snapshot_date", snapshot_date_type, nullable=False),
        sa.Column("portfolio_value_paise", sa.BigInteger(), nullable=False),
        sa.Column("invested_capital_paise", sa.BigInteger(), nullable=False),
        sa.Column(
            "net_cashflow_paise", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("twr_index_micros", sa.BigInteger(), nullable=False),
        sa.Column("benchmark_value_paise", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint(
            "user_id",
            "scope_type",
            "scope_id",
            "snapshot_date",
            name="pk_portfolio_daily_snapshots",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_portfolio_daily_snapshots_user_id_users",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "scope_type IN ('all', 'account', 'member')",
            name="scope_type_valid",
        ),
        **table_kwargs,
    )


def downgrade() -> None:
    op.drop_table("portfolio_daily_snapshots")
