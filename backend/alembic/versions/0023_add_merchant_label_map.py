"""add merchant_label_map for F3a Phase 2 (auto-learn merchant→label)

Revision ID: 0023_add_merchant_label_map
Revises: 0022_drop_transaction_note
Create Date: 2026-07-20

Lands the merchant→label memory table (PRD §F3a Phase 2). Sibling to
``merchant_tag_map`` (merchant→category), but an owned link between two owned
rows, so it uses the **ADR-0002 composite same-user FK**: ``(label_id, user_id) →
labels(id, user_id)`` (target ``uq_labels_id_user`` created by 0021). The FK is
``ON DELETE CASCADE`` because ``labels`` hard-delete — the composite target keeps
that cascade tenant-safe. ``user_id``'s integrity to ``users`` rides that
composite FK (``labels`` FKs ``users``), so no direct ``users`` FK is declared.

Hand-written so constraint / index names match the SA NAMING_CONVENTION;
``tests/test_migration_parity.py`` guards drift against the model.
``op.create_table`` does not emit ``__table_args__`` indexes, hence the standalone
``create_index`` call.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023_add_merchant_label_map"
down_revision: str | Sequence[str] | None = "0022_drop_transaction_note"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "merchant_label_map",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("merchant_normalized", sa.String(length=512), nullable=False),
        sa.Column("label_id", sa.Integer(), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("last_used", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["label_id", "user_id"],
            ["labels.id", "labels.user_id"],
            name="fk_merchant_label_map_label_id_labels",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_merchant_label_map"),
        sa.UniqueConstraint(
            "user_id",
            "merchant_normalized",
            "label_id",
            name="uq_merchant_label_map_user_merchant_label",
        ),
    )
    op.create_index(
        "ix_merchant_label_map_user_merchant",
        "merchant_label_map",
        ["user_id", "merchant_normalized"],
    )


def downgrade() -> None:
    op.drop_index("ix_merchant_label_map_user_merchant", table_name="merchant_label_map")
    op.drop_table("merchant_label_map")
