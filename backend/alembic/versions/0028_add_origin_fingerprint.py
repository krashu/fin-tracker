"""add transactions.origin_fingerprint (ADR-0007 rule 9 provenance)

Revision ID: 0028_add_origin_fingerprint
Revises: 0027_investment_fingerprint_separator_and_occurrence
Create Date: 2026-08-08

ADR-0007 widens ``PATCH /transactions`` to every user-visible column, so the four
ADR-0006 identity inputs (date / amount / merchant / account) become editable and
``fingerprint`` is recomputed on an edit. Without a second stored hash the importer
would read that edit as a *deletion* and re-stage the pre-edit row on the next
upload of the same statement, producing two rows for one real transaction whose
fingerprints differ **by construction** — a duplicate F4 can never detect.

``origin_fingerprint`` answers *which statement line produced this row*, frozen at
stage time and never recomputed; ``fingerprint`` keeps answering *what does this
row say* and stays the unique-constrained one. The file-dedup prefetches key on
``COALESCE(origin_fingerprint, fingerprint)``, so an edited row still matches its
own source line. NULL means "no external source line": manual entry, both transfer
legs, F4a, the demo seeder, backup CSV import. Neither unique nor indexed
(ADR-0007 rule 9) — the prefetches are bounded by the parsed file's fingerprint set.

Plain ``add_column`` (not ``batch_alter_table``): SQLite ``ALTER TABLE ADD COLUMN``
accepts a nullable unconstrained column, so no table rebuild is needed and none of
``0025``'s self-referential-composite-FK hazards apply.

**The downgrade is one-way for already-edited rows** (ADR-0006 §Recompute e).
Dropping the column is lossless only while ``origin_fingerprint = fingerprint``;
once a PATCH has recomputed ``fingerprint``, the pre-edit value is not computable
from any still-stored column and the provenance is gone for good.

Backfill caveat: the predicate is ``source = 'import'``, which is the best signal
the schema carries but is **not** exactly "produced by the statement importer" —
``backup_import_service`` replays the exported ``source`` value, so a backup-restored
row also reads ``'import'`` and is stamped here even though ADR-0007 rule 9 keeps
backup rows NULL at runtime. Harmless: an unedited row has
``origin_fingerprint == fingerprint``, so ``COALESCE`` yields the same key either
way, and the two only diverge once such a row is edited — where freezing the
pre-edit hash is the behaviour rule 9 wants anyway.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0028_add_origin_fingerprint"
down_revision: str | Sequence[str] | None = "0027_investment_fingerprint_separator_and_occurrence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column("origin_fingerprint", sa.String(length=64), nullable=True),
    )
    # Imported rows are equal on both hashes at birth; they diverge only on a later
    # edit. Manual rows stay NULL deliberately (rule 9): a manual row has no external
    # artifact, so its own current assertion is the only honest dedup key.
    op.execute(
        sa.text("UPDATE transactions SET origin_fingerprint = fingerprint WHERE source = 'import'")
    )


def downgrade() -> None:
    op.drop_column("transactions", "origin_fingerprint")
