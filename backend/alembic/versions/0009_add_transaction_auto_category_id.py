"""transactions.auto_category_id: frozen import auto-tag suggestion

Revision ID: 0009_add_transaction_auto_category_id
Revises: 0008_category_kind
Create Date: 2026-06-18

Adds a nullable ``auto_category_id`` FK to ``transactions`` recording the
category the import auto-tag (PRD §F3) prefilled at ingest. It freezes the
suggestion so the acceptance-rate metric (``GET /dashboards/tagging-stats``,
PRD §Success-metrics: ≥80% pre-tagged correctly) can compare it against the
final ``category_id`` — "kept the suggestion" precision.

Adding a column *with a ForeignKey* needs ``batch_alter_table`` on SQLite —
a plain ``op.add_column`` raises "No support for ALTER of constraints in SQLite
dialect" (the FK is a table constraint). Batch's copy-and-move rebuild is safe
here: the composite-unique target ``uq_transactions_id_user`` already exists
(migration 0005), so the self-referential composite FK validates at copy time
(the 0005 trap doesn't recur). SQLAlchemy 2.0 reflects the partial-index WHERE
(``ix_transactions_user_confirmed_date``), so the batch rebuild preserves it —
verified by ``test_partial_index_where_clause_preserved``.

No backfill: pre-existing rows get NULL and so are correctly excluded from the
metric's denominator (historical suggestions are unreconstructable). The FK name
matches ``base.py`` NAMING_CONVENTION (``fk_<table>_<col>_<reftable>``); the
parity test compares FKs by columns/referred-table, but the explicit name keeps
the DDL honest on Postgres.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_add_transaction_auto_category_id"
down_revision: str | Sequence[str] | None = "0008_category_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "auto_category_id",
                sa.Integer(),
                sa.ForeignKey(
                    "categories.id",
                    name="fk_transactions_auto_category_id_categories",
                ),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_column("auto_category_id")
