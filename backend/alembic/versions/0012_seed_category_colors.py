"""seed default category colors

Revision ID: 0012_seed_category_colors
Revises: 0011_category_color
Create Date: 2026-06-21

Backfills a default ``color`` onto the built-in seed categories (the 0003 spend
defaults + the 0008 income defaults). Those rows were inserted before the
``color`` column existed (added in 0011), so they all read back as "No color";
this gives each a sensible starting hex the user can change in settings.

Matched by ``name`` + ``is_seeded`` (the seeds have known names) and guarded by
``color IS NULL`` so it never clobbers a color a user already picked. Colors are
just defaults — not unique, not enforced. New installs reach the same end state:
0003/0008 insert the seeds, 0011 adds the column, 0012 colors them.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_seed_category_colors"
down_revision: str | Sequence[str] | None = "0011_category_color"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Default hex per seed category name (0003 spend + 0008 income). Lower-case to
# match the schema's normalization. "Other" exists in both scopes and both get
# this neutral grey — fine for a default.
_SEED_COLORS: dict[str, str] = {
    # 0003 spend defaults
    "Food": "#f97316",
    "Groceries": "#84cc16",
    "Transport": "#3b82f6",
    "Rent": "#8b5cf6",
    "Utilities": "#06b6d4",
    "Shopping": "#ec4899",
    "Entertainment": "#a855f7",
    "Health": "#ef4444",
    "Travel": "#14b8a6",
    "Subscriptions": "#6366f1",
    "EMI": "#f43f5e",
    "Investment": "#10b981",
    "Income": "#22c55e",  # vestigial flat seed (archived by 0008)
    "Transfer": "#64748b",  # vestigial flat seed (archived by 0008)
    "Other": "#94a3b8",
    # 0008 income defaults
    "Salary": "#16a34a",
    "Freelancing": "#0ea5e9",
    "Cashback": "#eab308",
}


def upgrade() -> None:
    bind = op.get_bind()
    for name, color in _SEED_COLORS.items():
        bind.execute(
            sa.text(
                "UPDATE categories SET color = :color "
                "WHERE name = :name AND is_seeded = :seeded AND color IS NULL"
            ).bindparams(color=color, name=name, seeded=True)
        )


def downgrade() -> None:
    bind = op.get_bind()
    # Revert only the defaults this migration set: a seed whose color still
    # equals its assigned default goes back to NULL; a user-changed color stays.
    for name, color in _SEED_COLORS.items():
        bind.execute(
            sa.text(
                "UPDATE categories SET color = NULL "
                "WHERE name = :name AND is_seeded = :seeded AND color = :color"
            ).bindparams(color=color, name=name, seeded=True)
        )
