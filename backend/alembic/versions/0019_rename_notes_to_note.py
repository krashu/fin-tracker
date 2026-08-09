"""rename transaction ``notes`` column to ``note`` on both txn tables

Revision ID: 0019_rename_notes_to_note
Revises: 0018_recolor_seed_categories
Create Date: 2026-07-19

Renames the free-text ``notes`` column to the singular ``note`` on both
``transactions`` and ``investment_transactions`` — one note per row, matching the
UI label. Column type / nullability are unchanged (``VARCHAR(1024)`` nullable);
this is a pure identifier rename.

Deliberately a raw ``op.execute("ALTER TABLE ... RENAME COLUMN ...")`` and **not**
``op.batch_alter_table``. ``alembic/env.py`` turns on ``render_as_batch`` globally
for SQLite, and a batch op DROP+recreates the table — which trips the
self-referential same-user FK on *both* tables (``transfer_pair_id`` on
``transactions``; the composite same-user FK on ``investment_transactions``).
Native ``RENAME COLUMN`` is a metadata-only op that avoids the rebuild entirely
and is portable across SQLite ≥ 3.25 (guaranteed by Python 3.13's bundled
``sqlite3``) and Postgres.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019_rename_notes_to_note"
down_revision: str | Sequence[str] | None = "0018_recolor_seed_categories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("transactions", "investment_transactions")


def upgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} RENAME COLUMN notes TO note")


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} RENAME COLUMN note TO notes")
