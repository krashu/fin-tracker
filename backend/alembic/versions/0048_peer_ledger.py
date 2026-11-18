"""add peer_contacts and peer_entries auxiliary peer ledger (ADR-0017 Phase 1)

Revision ID: 0048_peer_ledger
Revises: 0047_retire_card_bill_category
Create Date: 2026-11-18

Peer debts and Khatabook balances (ADR-0017):
- ``peer_contacts`` tracks friends and personal contacts with partial unique active names.
- ``peer_entries`` tracks receivables ('you_gave') and payables ('you_got'), optionally
  linked to a parent transaction.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0048_peer_ledger"
down_revision: str | Sequence[str] | None = "0047_retire_card_bill_category"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIRECTION_ENUM = sa.Enum(
    "you_gave",
    "you_got",
    name="peer_direction",
    native_enum=False,
    create_constraint=True,
    validate_strings=True,
)


def upgrade() -> None:
    op.create_table(
        "peer_contacts",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("upi_id", sa.String(length=100), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_peer_contacts"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_peer_contacts_user_id_users",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("id", "user_id", name="uq_peer_contacts_user_id_id"),
    )
    op.create_index(
        "uq_peer_contacts_active_user_name",
        "peer_contacts",
        ["user_id", sa.text("LOWER(TRIM(name))")],
        unique=True,
        sqlite_where=sa.text("archived_at IS NULL"),
        postgresql_where=sa.text("archived_at IS NULL"),
    )

    op.create_table(
        "peer_entries",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("direction", _DIRECTION_ENUM, nullable=False),
        sa.Column("is_settlement", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_peer_entries"),
        sa.ForeignKeyConstraint(
            ["contact_id", "user_id"],
            ["peer_contacts.id", "peer_contacts.user_id"],
            name="fk_peer_entries_contact_id_peer_contacts",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id", "user_id"],
            ["transactions.id", "transactions.user_id"],
            name="fk_peer_entries_transaction_id_transactions",
            ondelete="NO ACTION",
        ),
        sa.CheckConstraint("amount_paise > 0", name="amount_positive"),
    )
    op.create_index("ix_peer_entries_user_contact", "peer_entries", ["user_id", "contact_id"])
    op.create_index("ix_peer_entries_user_txn", "peer_entries", ["user_id", "transaction_id"])
    op.create_index("ix_peer_entries_user_date", "peer_entries", ["user_id", "date"])


def downgrade() -> None:
    op.drop_index("ix_peer_entries_user_date", table_name="peer_entries")
    op.drop_index("ix_peer_entries_user_txn", table_name="peer_entries")
    op.drop_index("ix_peer_entries_user_contact", table_name="peer_entries")
    op.drop_table("peer_entries")
    op.drop_index("uq_peer_contacts_active_user_name", table_name="peer_contacts")
    op.drop_table("peer_contacts")
