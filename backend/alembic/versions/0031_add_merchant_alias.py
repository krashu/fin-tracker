"""add merchant_alias table (Stage A, Phase A1 -- merchant-alias layer)

Revision ID: 0031_add_merchant_alias
Revises: 0030_add_import_batch_reconciliation
Create Date: 2026-08-12

Table only -- no seed data (Phase A5) and nothing reads or writes this table
yet (Phase A2). Sibling shape to 0023's merchant_label_map: hand-written so
constraint / index names match NAMING_CONVENTION; test_migration_parity.py
guards drift against the model. op.create_table does not emit __table_args__
indexes, hence the standalone create_index call.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0031_add_merchant_alias"
down_revision: str | Sequence[str] | None = "0030_add_import_batch_reconciliation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "merchant_alias",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("pattern", sa.String(length=512), nullable=False),
        sa.Column("canonical", sa.String(length=512), nullable=False),
        sa.Column("is_seeded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_merchant_alias_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_merchant_alias"),
        sa.UniqueConstraint("user_id", "pattern", name="uq_merchant_alias_user_pattern"),
    )
    op.create_index("ix_merchant_alias_user_id", "merchant_alias", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_merchant_alias_user_id", table_name="merchant_alias")
    op.drop_table("merchant_alias")
