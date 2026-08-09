"""add label tables (labels, transaction_labels) for F3a user tags

Revision ID: 0021_add_transaction_labels
Revises: 0020_widen_merchant_normalized
Create Date: 2026-07-20

Lands the F3a transaction-label data model (PRD §F3a): ``labels`` (owned,
per-user user tags) and ``transaction_labels`` (many-to-many join). Additive
only — the free-text ``transactions.note`` column is dropped in a *separate*
follow-up migration (0022) so this create-tables step stays independently
reviewable and green.

Both hand-written so constraint / index names match the SA NAMING_CONVENTION;
``tests/test_migration_parity.py`` guards drift against the models.

**Composite same-user FK (ADR-0002 pattern).** ``transaction_labels`` links two
owned rows, so ``user_id`` rides both composite FKs — ``(transaction_id,
user_id) → transactions(id, user_id)`` and ``(label_id, user_id) → labels(id,
user_id)`` — making a cross-tenant link impossible at the DB level. Both FK
targets need a composite unique index, so ``labels`` gets
``uq_labels_id_user`` created *before* ``transaction_labels`` (``transactions``
already carries ``uq_transactions_id_user`` from migration 0005). Both FKs are
``ON DELETE CASCADE`` (SQLite honours it under ``PRAGMA foreign_keys=ON``), so a
transaction or label delete auto-clears its link rows. ``op.create_table`` does
not emit ``__table_args__`` indexes, hence the standalone ``create_index`` calls.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0021_add_transaction_labels"
down_revision: str | Sequence[str] | None = "0020_widen_merchant_normalized"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------- labels
    op.create_table(
        "labels",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_labels_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_labels"),
        sa.UniqueConstraint("user_id", "name", name="uq_labels_user_name"),
    )
    # Composite-unique target the transaction_labels same-user FK needs (Postgres
    # requires a unique index on the referenced (id, user_id) tuple).
    op.create_index("uq_labels_id_user", "labels", ["id", "user_id"], unique=True)

    # ------------------------------------------------------- transaction_labels
    op.create_table(
        "transaction_labels",
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("label_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # TimestampMixin columns. server_default is load-bearing: the mixin has no
        # Python-side default and set_labels_on_transaction INSERTs these rows
        # without the timestamp columns, so the DB default must fill them.
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["transaction_id", "user_id"],
            ["transactions.id", "transactions.user_id"],
            name="fk_transaction_labels_transaction_id_transactions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["label_id", "user_id"],
            ["labels.id", "labels.user_id"],
            name="fk_transaction_labels_label_id_labels",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("transaction_id", "label_id", name="pk_transaction_labels"),
    )
    op.create_index(
        "ix_transaction_labels_user_label",
        "transaction_labels",
        ["user_id", "label_id"],
    )


def downgrade() -> None:
    # Reverse dependency order: transaction_labels FKs labels.
    op.drop_index("ix_transaction_labels_user_label", table_name="transaction_labels")
    op.drop_table("transaction_labels")
    op.drop_index("uq_labels_id_user", table_name="labels")
    op.drop_table("labels")
