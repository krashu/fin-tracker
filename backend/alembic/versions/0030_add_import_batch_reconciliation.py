"""add statement-summary + reconciliation-delta columns to import_batches

Revision ID: 0030_add_import_batch_reconciliation
Revises: 0029_refund_as_signed_spend
Create Date: 2026-08-12

Five nullable columns (PRD §F1/§F4a, balance-reconciliation). ``ImportBatch``
gains the statement's own opening/closing balance and period, plus the
computed window-delta verdict — see the model docstring for what each column
means and ``app/services/reconciliation_service.reconcile_batch`` for how the
delta is computed.

Plain ``add_column`` (not ``batch_alter_table``): all five are nullable with
no default, so SQLite's ``ALTER TABLE ADD COLUMN`` handles them without a
table rebuild — no risk to ``import_batches``' existing FK / index shape.

No backfill. Every pre-existing batch legitimately reads as "not checked"
(``reconciliation_delta_paise IS NULL``) rather than "reconciled" — it was
never checked, so a `0` would be a false claim of agreement.

Deliberately **not** a ``reconciliation_status`` enum column —
``reconciliation_delta_paise`` alone carries NULL="not checked", 0="reconciled",
non-zero="mismatch, this many paise (actual − expected, signed)". An enum's
CHECK constraint (``create_constraint=True``, ADR-0001 rule 2) cannot be added
to SQLite via plain ``ADD COLUMN``, and status is cheap to derive at read time.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0030_add_import_batch_reconciliation"
down_revision: str | Sequence[str] | None = "0029_refund_as_signed_spend"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "import_batches",
        sa.Column("statement_opening_balance_paise", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "import_batches",
        sa.Column("statement_closing_balance_paise", sa.BigInteger(), nullable=True),
    )
    op.add_column("import_batches", sa.Column("period_start", sa.Date(), nullable=True))
    op.add_column("import_batches", sa.Column("period_end", sa.Date(), nullable=True))
    op.add_column(
        "import_batches",
        sa.Column("reconciliation_delta_paise", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("import_batches", "reconciliation_delta_paise")
    op.drop_column("import_batches", "period_end")
    op.drop_column("import_batches", "period_start")
    op.drop_column("import_batches", "statement_closing_balance_paise")
    op.drop_column("import_batches", "statement_opening_balance_paise")
