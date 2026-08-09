"""add pinned flag to merchant_tag_map + merchant_label_map (F3/F3a rule authoring)

Revision ID: 0024_add_pinned_to_merchant_maps
Revises: 0023_add_merchant_label_map
Create Date: 2026-07-21

Adds a ``pinned`` boolean to both learned-memory tables so the user can author /
pin a merchant→category (F3) or merchant→label (F3a) rule that outranks
higher-``hit_count`` learned rows (the ``/settings/rules`` authoring feature). A
pinned row wins its reducer regardless of ``hit_count``; the learning path never
sets the flag, and pin/un-pin toggles only this column (never ``hit_count`` /
``last_used``), so un-pinning reverts cleanly to the learned ranking.

Plain ``add_column`` (not ``batch_alter_table``): SQLite ``ALTER TABLE ADD
COLUMN`` accepts a NOT NULL column with a *constant* default, so no table rebuild
is needed. Batch would rebuild ``merchant_label_map`` and force faithfully
reconstructing its ADR-0002 composite FK / unique / index (guarded by
``tests/test_migration_parity.py``). ``sa.false()`` renders portably — ``0`` on
SQLite, ``false`` on Postgres.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024_add_pinned_to_merchant_maps"
down_revision: str | Sequence[str] | None = "0023_add_merchant_label_map"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "merchant_tag_map",
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "merchant_label_map",
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("merchant_label_map", "pinned")
    op.drop_column("merchant_tag_map", "pinned")
