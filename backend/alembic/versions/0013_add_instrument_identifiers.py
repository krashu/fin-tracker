"""instruments.isin / amfi_code: identity keys for NAV/price matching

Revision ID: 0013_add_instrument_identifiers
Revises: 0012_seed_category_colors
Create Date: 2026-06-21

Adds two nullable identity columns to ``instruments`` (PRD §F7):

* ``isin`` — the 12-char ISO 6166 identifier the broker CSV already ships but the
  importer previously discarded. Keys an Indian MF to its AMFI NAVAll row and helps
  match an Indian equity to its quote (v0.6.5 NAV/price snapshot).
* ``amfi_code`` — the AMFI scheme code. NULL until the NAVAll snapshot back-fills it
  on first MF match; ``String(16)`` is a safe pad over the ~6-digit codes.

``symbol`` stays the dedup key — these are additive metadata, not the identity.

Plain ``op.add_column`` (mirrors 0011): a constraint-free, nullable column add that
SQLite's ``ALTER TABLE ADD COLUMN`` supports directly. The downgrade drops via
``batch_alter_table`` for SQLite portability.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_add_instrument_identifiers"
down_revision: str | Sequence[str] | None = "0012_seed_category_colors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("instruments", sa.Column("isin", sa.String(length=12), nullable=True))
    op.add_column("instruments", sa.Column("amfi_code", sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("instruments") as batch_op:
        batch_op.drop_column("amfi_code")
        batch_op.drop_column("isin")
