"""add linked_spend_txn_id to investment_transactions and seed system:investment_transfer category

Revision ID: 0051_spend_seam_and_investment_transfer
Revises: 0050_investment_accounts_and_attribution
Create Date: 2026-11-21

Spend Seam & Bank SIP Transfer Linkage (Milestone 1 Step 3):
1. Adds nullable column ``linked_spend_txn_id`` to ``investment_transactions`` with
   composite same-user foreign key:
   (linked_spend_txn_id, user_id) -> transactions(id, user_id) ON DELETE SET NULL.
2. Creates index ``ix_investment_transactions_user_linked_spend`` on
   (user_id, linked_spend_txn_id).
3. Seeds the root category ``system:investment_transfer`` (kind='spend', is_seeded=True,
   color='#64748b') for every user in ``users`` table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0051_spend_seam_and_investment_transfer"
down_revision: str | Sequence[str] | None = "0050_investment_accounts_and_attribution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add linked_spend_txn_id and composite FK to transactions
    op.add_column(
        "investment_transactions",
        sa.Column("linked_spend_txn_id", sa.Integer(), nullable=True),
    )
    with op.batch_alter_table("investment_transactions") as batch_op:
        batch_op.create_foreign_key(
            "fk_investment_transactions_linked_spend_txn_id_transactions",
            "transactions",
            ["linked_spend_txn_id", "user_id"],
            ["id", "user_id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_investment_transactions_user_linked_spend",
            ["user_id", "linked_spend_txn_id"],
        )

    # 2. Seed system:investment_transfer category for all existing users
    bind = op.get_bind()
    users_table = sa.table("users", sa.column("id", sa.Uuid()))
    categories_table = sa.table(
        "categories",
        sa.column("id", sa.Integer()),
        sa.column("user_id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("kind", sa.String()),
        sa.column("is_seeded", sa.Boolean()),
        sa.column("color", sa.String()),
        sa.column("parent_id", sa.Integer()),
        sa.column("archived_at", sa.DateTime()),
    )
    user_rows = bind.execute(sa.select(users_table.c.id)).fetchall()
    for (u_id,) in user_rows:
        exists = bind.scalar(
            sa.select(categories_table.c.id).where(
                categories_table.c.user_id == u_id,
                categories_table.c.name == "system:investment_transfer",
                categories_table.c.kind == "spend",
            )
        )
        if not exists:
            bind.execute(
                categories_table.insert().values(
                    user_id=u_id,
                    name="system:investment_transfer",
                    kind="spend",
                    is_seeded=True,
                    color="#64748b",
                    parent_id=None,
                )
            )


def downgrade() -> None:
    # 1. Remove seeded system:investment_transfer category
    bind = op.get_bind()
    categories_table = sa.table(
        "categories",
        sa.column("name", sa.String()),
        sa.column("kind", sa.String()),
    )
    bind.execute(
        categories_table.delete().where(
            categories_table.c.name == "system:investment_transfer",
            categories_table.c.kind == "spend",
        )
    )

    # 2. Drop composite FK, index, and column from investment_transactions
    with op.batch_alter_table("investment_transactions") as batch_op:
        batch_op.drop_index("ix_investment_transactions_user_linked_spend")
        batch_op.drop_constraint(
            "fk_investment_transactions_linked_spend_txn_id_transactions",
            type_="foreignkey",
        )
        batch_op.drop_column("linked_spend_txn_id")
