"""categories.color: user-selectable palette token

Revision ID: 0011_category_color
Revises: 0010_cas_import
Create Date: 2026-06-21

Adds a nullable ``color`` column to ``categories`` holding the ``#rrggbb`` hex a
user picked for the category's dot/bar. NULL is the meaningful sentinel for
"derive the color from the id" (the Auto fallback, see
``frontend/lib/categories.ts``), so:

* **No backfill.** Pre-existing rows and the 0003 seed defaults stay NULL and
  keep deriving — the picker only writes a token when the user chooses one.
* **No ``server_default``.** NULL is intended, not a gap to fill.

Plain ``op.add_column`` — unlike 0009 (which added an *FK* and so needed
``batch_alter_table``), this is a constraint-free column add that SQLite's
``ALTER TABLE ADD COLUMN`` supports directly. The downgrade drops via
``batch_alter_table`` for SQLite portability.

Hand-written (not autogenerate): the ``categories`` table carries the partial
unique index ``uq_categories_active_user_name`` whose WHERE clause autogenerate
mangles (see ``app/models/category.py``). The hex shape is validated at the
Pydantic boundary (``CategoryCreate``/``CategoryUpdate``), not in the DB — a
plain ``String(16)`` keeps the column portable.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_category_color"
down_revision: str | Sequence[str] | None = "0010_cas_import"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("categories", sa.Column("color", sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("categories") as batch_op:
        batch_op.drop_column("color")
