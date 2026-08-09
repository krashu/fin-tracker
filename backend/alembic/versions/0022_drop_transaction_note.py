"""drop transactions.note — F3a labels replace the free-text note

Revision ID: 0022_drop_transaction_note
Revises: 0021_add_transaction_labels
Create Date: 2026-07-20

F3a transaction labels (``labels`` / ``transaction_labels``, migration 0021)
replace the per-row free-text ``note`` on ``transactions``. This physically
drops the column — data loss is accepted (pre-release; per the approved plan a
Backup export is the archive path). ``investment_transactions.note`` is
**untouched** (investments keep freeform notes).

Deliberately a raw ``op.execute("ALTER TABLE ... DROP COLUMN ...")`` and **not**
``op.drop_column``. ``alembic/env.py`` turns on ``render_as_batch`` globally for
SQLite, and a batch op DROP+recreates the table — which trips the
self-referential same-user FK ``fk_transactions_transfer_pair_same_user`` on the
copy step (see migration 0005). Native ``DROP COLUMN`` is a metadata-only op
(``note`` is in no index / constraint / CHECK) and is portable across SQLite
≥ 3.35 (guaranteed by Python 3.13's bundled ``sqlite3``) and Postgres — the same
approach migration 0019 used for RENAME COLUMN.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0022_drop_transaction_note"
down_revision: str | Sequence[str] | None = "0021_add_transaction_labels"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE transactions DROP COLUMN note")


def downgrade() -> None:
    # Restores the column shape (VARCHAR(1024) nullable); the original free-text
    # content is not recoverable — the drop is destructive by design.
    op.execute("ALTER TABLE transactions ADD COLUMN note VARCHAR(1024)")
