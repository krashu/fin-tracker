"""add investment tables (instruments, investment_transactions)

Revision ID: 0007_add_investment_tables
Revises: 0006_merchant_raw_nullable
Create Date: 2026-06-17

Lands the F7 investment data model (PRD §F7): ``instruments`` (one row per
scheme/ticker, no ``account_id`` — investments are decoupled from the spend
tables) and ``investment_transactions`` (buys / sells / dividends etc.).

Hand-written so constraint / index names match the SA NAMING_CONVENTION;
``tests/test_migration_parity.py`` guards against drift on every pytest run.

**Scaled-int storage.** ``units`` / ``price_per_unit_native`` / ``current_nav`` /
``fx_rate_to_inr`` are exact decimals stored as scaled ``int64`` via the
``ScaledDecimal`` TypeDecorator in ``app/models/types.py`` — but a TypeDecorator
over ``BigInteger`` introspects as ``BIGINT``, so the storage shape here is plain
``sa.BigInteger()`` (the decorator is Python-semantics only). The scaled
``server_default`` for ``fx_rate_to_inr`` is the *scaled* int: ``1.0`` → ``1000000``.

**Self-referential composite FK.** ``investment_transactions.switch_pair_id`` mirrors
``transactions.transfer_pair_id`` (same-user composite FK + no-self-pair CHECK +
composite-unique target index). Unlike migration 0005 — which had to use the
batch-alter dance to add the composite FK to an *existing* populated table — this
table is brand new, so the composite FK is declared inline in ``create_table`` and
the composite-unique target index is created immediately after (no rows exist, so
SQLite never validates the FK at upgrade time). This matches what
``Base.metadata.create_all`` emits for ``transactions``.

INR-only this slice: USD instruments + an ``fx_rates`` table land in v0.5.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_add_investment_tables"
down_revision: str | Sequence[str] | None = "0006_merchant_raw_nullable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------ instruments
    op.create_table(
        "instruments",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column(
            "asset_class",
            sa.Enum(
                "indian_equity",
                "indian_mf",
                "us_equity",
                "us_etf",
                "fd",
                "bond",
                "nps",
                "gold",
                "other",
                name="asset_class",
                native_enum=False,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "currency",
            sa.Enum(
                "INR",
                "USD",
                name="currency",
                native_enum=False,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=False,
            server_default=sa.text("'INR'"),
        ),
        sa.Column(
            "exchange",
            sa.Enum(
                "NSE",
                "BSE",
                "MFCentral",
                "NASDAQ",
                "NYSE",
                "OTHER",
                name="exchange",
                native_enum=False,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("current_nav", sa.BigInteger(), nullable=True),
        sa.Column("nav_updated_at", sa.DateTime(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_instruments_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_instruments"),
    )
    # Active (non-archived) symbols unique per user; archived rows may repeat.
    op.create_index(
        "uq_instruments_active_user_symbol",
        "instruments",
        ["user_id", "symbol"],
        unique=True,
        sqlite_where=sa.text("archived_at IS NULL"),
        postgresql_where=sa.text("archived_at IS NULL"),
    )

    # ------------------------------------------- investment_transactions
    op.create_table(
        "investment_transactions",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column(
            "transaction_type",
            sa.Enum(
                "buy",
                "sell",
                "sip",
                "dividend",
                "bonus",
                "split",
                "switch_in",
                "switch_out",
                name="investment_transaction_type",
                native_enum=False,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("units", sa.BigInteger(), nullable=False),
        sa.Column("price_per_unit_native", sa.BigInteger(), nullable=True),
        sa.Column("amount_native_paise", sa.BigInteger(), nullable=False),
        sa.Column(
            "fees_native_paise", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "fx_rate_to_inr",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("1000000"),  # scaled 1.0 (FxRate scale 1e6)
        ),
        sa.Column("notes", sa.String(length=1024), nullable=True),
        sa.Column("switch_pair_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_investment_transactions_user_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name="fk_investment_transactions_instrument_id_instruments",
        ),
        sa.ForeignKeyConstraint(
            ["switch_pair_id", "user_id"],
            ["investment_transactions.id", "investment_transactions.user_id"],
            name="fk_investment_transactions_switch_pair_same_user",
        ),
        sa.CheckConstraint(
            "switch_pair_id IS NULL OR switch_pair_id != id",
            name="ck_investment_transactions_no_self_pair",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_investment_transactions"),
    )
    # Composite-unique target for the same-user self-referential composite FK
    # (mirrors uq_transactions_id_user — declared as a unique Index, not a
    # UniqueConstraint, to match the model's __table_args__ shape).
    op.create_index(
        "uq_investment_transactions_id_user",
        "investment_transactions",
        ["id", "user_id"],
        unique=True,
    )
    # Backs the holdings FIFO read: per-instrument, (date, id) ascending.
    op.create_index(
        "ix_investment_transactions_user_instrument_date",
        "investment_transactions",
        ["user_id", "instrument_id", "date", "id"],
    )


def downgrade() -> None:
    # Reverse dependency order: investment_transactions FKs instruments.
    op.drop_table("investment_transactions")
    op.drop_table("instruments")
