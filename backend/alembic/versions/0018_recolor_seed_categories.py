"""recolor default category colors to the validated palette

Revision ID: 0018_recolor_seed_categories
Revises: 0017_multiuser_auth
Create Date: 2026-07-18

Re-points the built-in seed categories (0003 spend + 0008 income, first coloured
by 0012) at the validated ``CATEGORY_PALETTE`` (frontend ``lib/categories.ts``).
The old defaults were a raw Tailwind-500 rainbow that failed the data-viz
lightness / chroma / CVD / contrast checks; the new hues are a single-hex set
validated on both the light (``#ffffff``) and dark (``#171c22``) chart surfaces.

Guarded by ``color = <old default>`` (plus ``name`` + ``is_seeded``) so it only
touches a seed still wearing its 0012 default — a colour the user picked
themselves is left alone. "Other" (both scopes) keeps its neutral grey and so is
absent from the map. The vestigial flat "Income" / "Transfer" seeds (archived by
0008) are intentionally untouched.

New installs reach the same end state without this migration:
:mod:`app.services.provisioning` seeds these same hues directly at registration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018_recolor_seed_categories"
down_revision: str | Sequence[str] | None = "0017_multiuser_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# name -> (old 0012 default, new palette hex). Guard on the old value so a
# user-customised colour is never clobbered. "Other" is unchanged (neutral grey)
# and omitted.
_RECOLOR: dict[str, tuple[str, str]] = {
    # 0003 spend defaults
    "Food": ("#f97316", "#d95926"),
    "Groceries": ("#84cc16", "#6f9e15"),
    "Transport": ("#3b82f6", "#2a78d6"),
    "Rent": ("#8b5cf6", "#6c5cd6"),
    "Utilities": ("#06b6d4", "#0e97c4"),
    "Shopping": ("#ec4899", "#d55181"),
    "Entertainment": ("#a855f7", "#b246c0"),
    "Health": ("#ef4444", "#e34948"),
    "Travel": ("#14b8a6", "#0e9488"),
    "Subscriptions": ("#6366f1", "#1baf7a"),
    "EMI": ("#f43f5e", "#c23b6b"),
    "Investment": ("#10b981", "#008300"),
    # 0008 income defaults
    "Salary": ("#16a34a", "#008300"),
    "Freelancing": ("#0ea5e9", "#2a78d6"),
    "Cashback": ("#eab308", "#c98500"),
}


def _remap(old_key: int, new_key: int) -> None:
    """Set ``color = colors[new_key]`` for each seed still wearing
    ``colors[old_key]`` — leaving user-picked colours untouched."""
    bind = op.get_bind()
    for name, colors in _RECOLOR.items():
        bind.execute(
            sa.text(
                "UPDATE categories SET color = :new "
                "WHERE name = :name AND is_seeded = :seeded AND color = :old"
            ).bindparams(
                new=colors[new_key],
                name=name,
                seeded=True,
                old=colors[old_key],
            )
        )


def upgrade() -> None:
    _remap(old_key=0, new_key=1)


def downgrade() -> None:
    _remap(old_key=1, new_key=0)
