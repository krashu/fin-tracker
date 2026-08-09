"""seed default categories

Revision ID: 0003_seed_default_categories
Revises: 0002_account_name_unique
Create Date: 2026-05-24

Seeds the 15 PRD §F5 default categories for the v1 user. Each row carries
``is_seeded=True`` so the downgrade can target only seeded rows without
nuking user-created categories that may have arrived between upgrade and
downgrade.

Data-only — the ``categories`` table itself is built in 0001.

WARNING: API tests use ``Base.metadata.create_all`` (see
``tests/api/conftest.py``) which does NOT run data migrations. Tests that
need the 15 seed rows must seed them manually via the ``seeded_categories``
fixture. The migration parity test in :mod:`tests.test_migration_parity`
DOES run ``alembic upgrade head`` and asserts this seed; that is the
load-bearing coverage.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_seed_default_categories"
down_revision: str | Sequence[str] | None = "0002_account_name_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Insertion order is documentary only — the GET endpoint orders by name ASC.
_DEFAULT_NAMES: tuple[str, ...] = (
    "Food",
    "Groceries",
    "Transport",
    "Rent",
    "Utilities",
    "Shopping",
    "Entertainment",
    "Health",
    "Travel",
    "Subscriptions",
    "EMI",
    "Investment",
    "Income",
    "Transfer",
    "Other",
)
_V1_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def upgrade() -> None:
    # ``is_seeded`` is set explicitly because the column server_default is
    # ``sa.false()`` (see app/models/category.py) — without the explicit
    # True, all 15 rows would seed as user-created. ``created_at`` and
    # ``updated_at`` are filled via the column-level ``server_default=now()``
    # from 0001 (verified by ``test_v1_user_seeded`` for the parallel pattern).
    categories_table = sa.table(
        "categories",
        sa.column("user_id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("is_seeded", sa.Boolean()),
    )
    op.bulk_insert(
        categories_table,
        [{"user_id": _V1_USER_ID, "name": n, "is_seeded": True} for n in _DEFAULT_NAMES],
    )


def downgrade() -> None:
    # Reversible: delete only seeded rows for the v1 user. User-created
    # rows (is_seeded=False) survive. Parameterised because ``sa.Uuid()``
    # stores hex-without-dashes on SQLite and native-uuid on Postgres —
    # a literal hyphenated string would miss SQLite rows.
    op.execute(
        sa.text("DELETE FROM categories WHERE is_seeded = TRUE AND user_id = :uid").bindparams(
            uid=_V1_USER_ID
        )
    )
