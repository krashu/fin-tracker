"""add investment_accounts and attribute transactions and realised_lots (ADR-0019)

Revision ID: 0050_investment_accounts_and_attribution
Revises: 0049_streamline_category_taxonomy
Create Date: 2026-11-20

Implements ADR-0019 (CBDT Circular No. 768 per-account FIFO and broker segmentation):
1. Creates table ``investment_accounts`` with composite unique target (id, user_id).
2. Adds column ``account_id`` to ``investment_transactions`` and ``realised_lots``.
3. Backfills a default "Default Portfolio" investment account for every user and
   links existing investment rows.
4. Uses batch_alter_table to set account_id NOT NULL and add composite same-user
   foreign keys (account_id, user_id) -> (investment_accounts.id, investment_accounts.user_id).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0050_investment_accounts_and_attribution"
down_revision: str | Sequence[str] | None = "0049_streamline_category_taxonomy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACCOUNT_TYPE_ENUM = sa.Enum(
    "demat",
    "mf_folio",
    "us_broker",
    "bank_fd",
    "nps",
    "manual",
    name="investment_account_type",
    native_enum=False,
    create_constraint=True,
    validate_strings=True,
)

_CURRENCY_ENUM = sa.Enum(
    "INR",
    "USD",
    name="investment_account_currency",
    native_enum=False,
    create_constraint=True,
    validate_strings=True,
)


def upgrade() -> None:
    # 1. Create investment_accounts table
    op.create_table(
        "investment_accounts",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("account_type", _ACCOUNT_TYPE_ENUM, nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("external_account_ref", sa.String(128), nullable=False),
        sa.Column("currency", _CURRENCY_ENUM, nullable=False, server_default="INR"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "user_id",
            "provider",
            "external_account_ref",
            name="uq_investment_accounts_user_provider_ref",
        ),
    )
    # Composite-unique reference target for same-user composite FKs
    op.create_index(
        "uq_investment_accounts_id_user",
        "investment_accounts",
        ["id", "user_id"],
        unique=True,
    )
    op.create_index(
        "ix_investment_accounts_user_id",
        "investment_accounts",
        ["user_id"],
    )

    # 2. Add nullable account_id to investment_transactions and realised_lots
    op.add_column("investment_transactions", sa.Column("account_id", sa.Integer(), nullable=True))
    op.add_column("realised_lots", sa.Column("account_id", sa.Integer(), nullable=True))

    # 3. Data backfill: Create default account for every user in users table
    bind = op.get_bind()
    users_table = sa.table("users", sa.column("id", sa.Uuid()))
    inv_accounts_table = sa.table(
        "investment_accounts",
        sa.column("id", sa.Integer()),
        sa.column("user_id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("account_type", sa.String()),
        sa.column("provider", sa.String()),
        sa.column("external_account_ref", sa.String()),
        sa.column("currency", sa.String()),
        sa.column("is_active", sa.Boolean()),
    )
    inv_txns_table = sa.table(
        "investment_transactions",
        sa.column("id", sa.Integer()),
        sa.column("user_id", sa.Uuid()),
        sa.column("account_id", sa.Integer()),
    )
    realised_lots_table = sa.table(
        "realised_lots",
        sa.column("id", sa.Integer()),
        sa.column("user_id", sa.Uuid()),
        sa.column("account_id", sa.Integer()),
    )

    user_rows = bind.execute(sa.select(users_table.c.id)).fetchall()
    for (u_id,) in user_rows:
        res = bind.execute(
            inv_accounts_table.insert().values(
                user_id=u_id,
                name="Default Portfolio",
                account_type="manual",
                provider="manual",
                external_account_ref="default",
                currency="INR",
                is_active=True,
            )
        )
        acc_id = res.inserted_primary_key[0] if res.inserted_primary_key else None
        if acc_id is None:
            # Fallback query if DB driver doesn't populate inserted_primary_key
            acc_id = bind.scalar(
                sa.select(inv_accounts_table.c.id).where(
                    inv_accounts_table.c.user_id == u_id,
                    inv_accounts_table.c.provider == "manual",
                    inv_accounts_table.c.external_account_ref == "default",
                )
            )

        bind.execute(
            inv_txns_table.update()
            .where(inv_txns_table.c.user_id == u_id)
            .values(account_id=acc_id)
        )
        bind.execute(
            realised_lots_table.update()
            .where(realised_lots_table.c.user_id == u_id)
            .values(account_id=acc_id)
        )

    # 4. Batch alter investment_transactions: enforce NOT NULL & composite FK
    with op.batch_alter_table("investment_transactions") as batch_op:
        batch_op.alter_column("account_id", nullable=False, existing_type=sa.Integer())
        batch_op.create_foreign_key(
            "fk_investment_transactions_account_id_investment_accounts",
            "investment_accounts",
            ["account_id", "user_id"],
            ["id", "user_id"],
        )
        batch_op.create_index(
            "ix_investment_transactions_user_account_instrument_date",
            ["user_id", "account_id", "instrument_id", "date", "id"],
        )

    # 5. Batch alter realised_lots: enforce NOT NULL & composite FK
    with op.batch_alter_table("realised_lots") as batch_op:
        batch_op.alter_column("account_id", nullable=False, existing_type=sa.Integer())
        batch_op.create_foreign_key(
            "fk_realised_lots_account_id_investment_accounts",
            "investment_accounts",
            ["account_id", "user_id"],
            ["id", "user_id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    with op.batch_alter_table("realised_lots") as batch_op:
        batch_op.drop_constraint(
            "fk_realised_lots_account_id_investment_accounts", type_="foreignkey"
        )
        batch_op.drop_column("account_id")

    with op.batch_alter_table("investment_transactions") as batch_op:
        batch_op.drop_index("ix_investment_transactions_user_account_instrument_date")
        batch_op.drop_constraint(
            "fk_investment_transactions_account_id_investment_accounts", type_="foreignkey"
        )
        batch_op.drop_column("account_id")

    op.drop_index("ix_investment_accounts_user_id", table_name="investment_accounts")
    op.drop_index("uq_investment_accounts_id_user", table_name="investment_accounts")
    op.drop_table("investment_accounts")
