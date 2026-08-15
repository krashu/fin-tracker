"""add guest user fields (is_guest, guest_expires_at)

Revision ID: 0036_add_guest_user_fields
Revises: 0035_update_merchant_dictionary_subcategories
Create Date: 2026-08-16

Adds ``is_guest`` (Boolean, default False) and ``guest_expires_at`` (DateTime, nullable)
to ``users`` to support ephemeral guest demo sandboxes, plus an index on
``guest_expires_at`` for efficient periodic cleanup queries.

Plain ``add_column`` / ``drop_column`` (not ``batch_alter_table``): SQLite ``ALTER TABLE ADD
COLUMN`` accepts constant defaults and nullable columns without table rebuild. Batch would
rebuild ``users`` via temporary table DROP which violates active foreign keys under SQLite
``PRAGMA foreign_keys=ON``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0036_add_guest_user_fields"
down_revision: str | Sequence[str] | None = "0035_update_merchant_dictionary_subcategories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "is_guest",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "guest_expires_at",
            sa.DateTime(),
            nullable=True,
        ),
    )
    op.create_index("ix_users_guest_expires_at", "users", ["guest_expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_users_guest_expires_at", table_name="users")
    op.drop_column("users", "guest_expires_at")
    op.drop_column("users", "is_guest")
