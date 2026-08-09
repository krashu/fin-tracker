"""widen merchant_normalized 256 → 512 on transactions + merchant_tag_map

Revision ID: 0020_widen_merchant_normalized
Revises: 0019_rename_notes_to_note
Create Date: 2026-07-19

``normalize_merchant`` (``" ".join(raw.lower().split())``) is length-unbounded and
``merchant_raw`` is ``String(512)``, but ``merchant_normalized`` was ``String(256)``
on both ``transactions`` and ``merchant_tag_map``. SQLite ignores ``VARCHAR`` length
(stores TEXT), so this was invisible in v1 — but on Postgres a parsed merchant that
normalizes to > 256 chars raises value-too-long and aborts the whole import. Widen
to 512 (≥ ``merchant_raw``, since lowercase + whitespace-collapse never lengthens a
string beyond a comfortable 512 bound) so the column can hold anything the parser
emits. The PRD §F4 fingerprint hashes the *full* normalized string, so the stored
key and the fingerprint input stay in agreement — do NOT instead truncate inside
``normalize_merchant`` (that would change every stored fingerprint).

SQLite can't ALTER a column type in place — ``batch_alter_table`` rebuilds each
table (same proven path as migrations 0006 / 0009 on ``transactions``; the batch
copy re-validates the self-referential composite FK against the existing
``uq_transactions_id_user`` target and preserves the partial index — covered by
``test_migration_matches_models`` and ``test_partial_index_where_clause_preserved``).
Pure widening: no value can violate the larger cap, so no backfill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020_widen_merchant_normalized"
down_revision: str | Sequence[str] | None = "0019_rename_notes_to_note"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("transactions", "merchant_tag_map"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column(
                "merchant_normalized",
                existing_type=sa.String(256),
                type_=sa.String(512),
                existing_nullable=False,
            )


def downgrade() -> None:
    # UNSAFE if any row's merchant_normalized exceeds 256 chars — SQLite keeps
    # the value (length unenforced), but a Postgres backend would truncate/reject.
    # A downgrade past this point assumes no over-long values were stored.
    for table in ("transactions", "merchant_tag_map"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column(
                "merchant_normalized",
                existing_type=sa.String(512),
                type_=sa.String(256),
                existing_nullable=False,
            )
