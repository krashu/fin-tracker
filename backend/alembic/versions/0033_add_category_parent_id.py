"""add parent_id to categories for two-level categorization

Revision ID: 0033_add_category_parent_id
Revises: 0032_seed_merchant_dictionary
Create Date: 2026-08-14

Adds a nullable ``parent_id`` FK column to ``categories`` to support a 2-level
hierarchy (Parent -> Subcategory), along with an index on (user_id, parent_id).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0033_add_category_parent_id"
down_revision: str | Sequence[str] | None = "0032_seed_merchant_dictionary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Drop the partial index FIRST so the batch table-rebuild below
    #    doesn't drop the partial index predicate (mirrors migration 0008).
    op.drop_index("uq_categories_active_user_name", table_name="categories")

    # 2. Add parent_id FK via batch_alter_table.
    with op.batch_alter_table("categories") as batch_op:
        batch_op.add_column(
            sa.Column(
                "parent_id",
                sa.Integer(),
                sa.ForeignKey(
                    "categories.id",
                    name="fk_categories_parent_id_categories",
                    ondelete="CASCADE",
                ),
                nullable=True,
            )
        )

    # 3. Recreate the partial unique index.
    op.create_index(
        "uq_categories_active_user_name",
        "categories",
        ["user_id", "name", "kind"],
        unique=True,
        sqlite_where=sa.text("archived_at IS NULL"),
        postgresql_where=sa.text("archived_at IS NULL"),
    )
    op.create_index("ix_categories_user_parent_id", "categories", ["user_id", "parent_id"])


def downgrade() -> None:
    op.drop_index("ix_categories_user_parent_id", table_name="categories")
    op.drop_index("uq_categories_active_user_name", table_name="categories")
    with op.batch_alter_table("categories") as batch_op:
        batch_op.drop_column("parent_id")
    op.create_index(
        "uq_categories_active_user_name",
        "categories",
        ["user_id", "name", "kind"],
        unique=True,
        sqlite_where=sa.text("archived_at IS NULL"),
        postgresql_where=sa.text("archived_at IS NULL"),
    )
