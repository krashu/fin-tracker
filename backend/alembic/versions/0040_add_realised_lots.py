"""add realised_lots, the persisted FIFO match (ADR-0015 rule 5)

Revision ID: 0040_add_realised_lots
Revises: 0039_fallback_roots_and_cashback_spend
Create Date: 2026-11-15

One row per (sell, lot): units, cost, proceeds share, sell-fee share and both dates.
Two composite same-user FKs to ``investment_transactions`` (target
``uq_investment_transactions_id_user``), both ``ON DELETE CASCADE`` because the rows are
derived and rebuilt per instrument on every investment write. ``user_id`` has no direct
``users`` FK; integrity rides on those composite FKs.

No backfill, no data step: the project has no users and dev data only, and the rows are
filled by the next write to each instrument.

Hand-written so constraint and index names match the naming convention; CHECK names are
suffix-only (ADR-0001 rule 3b) and render as ``ck_realised_lots_<suffix>``.
``op.create_table`` does not emit model-level indexes, hence the standalone ``create_index``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0040_add_realised_lots"
down_revision: str | Sequence[str] | None = "0039_fallback_roots_and_cashback_spend"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CK_UNITS = "units > 0"
_CK_COST = "cost_native_paise >= 0"
_CK_PROCEEDS = "proceeds_native_paise >= 0"
_CK_SELL_FEES = "sell_fees_native_paise >= 0"
_CK_DATES = "buy_date <= sell_date"


def upgrade() -> None:
    op.create_table(
        "realised_lots",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("sell_txn_id", sa.Integer(), nullable=False),
        sa.Column("buy_txn_id", sa.Integer(), nullable=False),
        sa.Column("units", sa.BigInteger(), nullable=False),
        sa.Column("cost_native_paise", sa.BigInteger(), nullable=False),
        sa.Column("proceeds_native_paise", sa.BigInteger(), nullable=False),
        sa.Column("sell_fees_native_paise", sa.BigInteger(), nullable=False),
        sa.Column("buy_date", sa.Date(), nullable=False),
        sa.Column("sell_date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["sell_txn_id", "user_id"],
            ["investment_transactions.id", "investment_transactions.user_id"],
            name="fk_realised_lots_sell_txn_id_investment_transactions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["buy_txn_id", "user_id"],
            ["investment_transactions.id", "investment_transactions.user_id"],
            name="fk_realised_lots_buy_txn_id_investment_transactions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_realised_lots"),
        sa.UniqueConstraint(
            "user_id", "sell_txn_id", "buy_txn_id", name="uq_realised_lots_user_sell_buy"
        ),
        sa.CheckConstraint(_CK_UNITS, name="positive_units"),
        sa.CheckConstraint(_CK_COST, name="non_negative_cost"),
        sa.CheckConstraint(_CK_PROCEEDS, name="non_negative_proceeds"),
        sa.CheckConstraint(_CK_SELL_FEES, name="non_negative_sell_fees"),
        sa.CheckConstraint(_CK_DATES, name="buy_not_after_sell"),
    )
    op.create_index("ix_realised_lots_user_buy", "realised_lots", ["user_id", "buy_txn_id"])


def downgrade() -> None:
    op.drop_index("ix_realised_lots_user_buy", table_name="realised_lots")
    op.drop_table("realised_lots")
