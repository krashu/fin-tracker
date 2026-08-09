"""account name partial unique

Revision ID: 0002_account_name_unique
Revises: 0001_initial_schema
Create Date: 2026-05-24

Adds a partial unique index on accounts.(user_id, name) for non-archived
rows, mirroring the existing uq_categories_active_user_name pattern. The
matching __table_args__ on the Account model lands in the same commit;
test_migration_parity guards drift on every pytest run.

Precondition: this upgrade fails with UNIQUE constraint violation if any
two non-archived accounts share (user_id, name). v1 has zero such rows
in practice (single-user, manually-seeded). If a future migration of an
existing DB hits this, dedupe manually before re-running.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_account_name_unique"
down_revision: str | Sequence[str] | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_accounts_active_user_name",
        "accounts",
        ["user_id", "name"],
        unique=True,
        sqlite_where=sa.text("archived_at IS NULL"),
        postgresql_where=sa.text("archived_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_accounts_active_user_name", table_name="accounts")
