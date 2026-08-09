"""make transactions.merchant_raw nullable (NULL = no merchant)

Revision ID: 0006_merchant_raw_nullable
Revises: 0005_adr0002_transfer_pair_constraints
Create Date: 2026-06-17

``merchant_raw`` NULL is the honest representation of "no merchant" for
manual rows that legitimately have none (PRD §F2). ``merchant_normalized``
stays ``NOT NULL``/``""`` — it's the derived dedup/match key that feeds the
PRD §F4 fingerprint hash and the reconciliation ``regex.search``, both of
which require a string. The dedup fingerprint is unchanged: it always
hashes the (string) ``merchant_normalized`` (``""`` when no merchant).

No backfill: existing rows already carry a non-null ``merchant_raw`` and
remain valid under the relaxed constraint.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_merchant_raw_nullable"
down_revision: str | Sequence[str] | None = "0005_adr0002_transfer_pair_constraints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite can't ALTER COLUMN constraints in place — batch mode rebuilds
    # the table so the relaxed NOT NULL applies portably.
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.alter_column(
            "merchant_raw",
            existing_type=sa.String(512),
            nullable=True,
        )


def downgrade() -> None:
    # UNSAFE if any row has merchant_raw IS NULL — restoring NOT NULL will
    # fail (or, on a backend that backfills, silently coerce). No backfill
    # here: a downgrade past this point assumes the caller has already
    # resolved or removed null rows.
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.alter_column(
            "merchant_raw",
            existing_type=sa.String(512),
            nullable=False,
        )
