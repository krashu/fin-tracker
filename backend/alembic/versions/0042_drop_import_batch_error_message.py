"""drop import_batches.error_message (ADR-0010 rule 13: a failed upload stores no batch)

Revision ID: 0042_drop_import_batch_error_message
Revises: 0041_portable_origin_inputs
Create Date: 2026-11-15

Nothing ever wrote the column, and a failed upload stores no batch, so the drop loses no data
the app wrote (pre-production, accepted).

Native ``DROP COLUMN`` rather than ``op.drop_column``, following migration 0022: the column is
in no index, constraint or CHECK, and ``alembic/env.py``'s global ``render_as_batch`` would
otherwise rebuild ``import_batches``, which needs 0037's drop-and-recreate of the inbound
composite FKs from ``transactions`` and ``investment_transactions``. SQLite >= 3.35 (bundled
with Python 3.14) and Postgres both support it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0042_drop_import_batch_error_message"
down_revision: str | Sequence[str] | None = "0041_portable_origin_inputs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE import_batches DROP COLUMN error_message")


def downgrade() -> None:
    # Restores the column shape only; every row comes back NULL.
    op.execute("ALTER TABLE import_batches ADD COLUMN error_message VARCHAR(1024)")
