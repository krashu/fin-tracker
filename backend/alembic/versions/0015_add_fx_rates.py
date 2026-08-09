"""add fx_rates table (daily INR<->USD rate cache)

Revision ID: 0015_add_fx_rates
Revises: 0014_add_benchmarks
Create Date: 2026-06-24

Lands the F7 FX layer's rate cache (PRD §Data model): ``fx_rates`` — daily currency-pair
snapshots, global reference data (no ``user_id``), backfilled from frankfurter.app by
``fx_service.refresh_fx_rates`` and read on the holdings / portfolio / ingest paths. Hand-written
so constraint / index names match the SA NAMING_CONVENTION; ``tests/test_migration_parity.py``
guards drift (and asserts this downgrade drops the table).

**Scaled-int storage.** ``rate`` is an exact decimal stored as a scaled ``int64`` via the
``FxRate`` TypeDecorator (scale 1e6) — but a TypeDecorator over ``BigInteger`` introspects as
``BIGINT``, so the storage shape here is plain ``sa.BigInteger()`` (mirroring 0007/0014).

**No seed.** Unlike the 0014 benchmark catalog, rates are runtime-backfilled (``POST /fx/refresh``),
not schema-coupled reference data — so there is nothing to seed here.

``from_currency`` / ``to_currency`` each carry their OWN enum name (they cannot share one:
the NAMING_CONVENTION would emit two CHECKs called ``ck_fx_rates_currency`` in this table,
which Postgres rejects with 42710 and SQLite tolerates). The ``CHECK (rate > 0)`` guards
against a poisoned non-positive seed (a positive FX rate scales to a positive int) and is
named ``rate_positive`` — bare, because the convention adds the ``ck_fx_rates_`` prefix.

EDITED IN PLACE after shipping (see the batch-2 remediation commit): both enum names were
``currency``, so a from-scratch Postgres ``upgrade head`` died inside THIS revision's
CREATE TABLE and no later revision could have repaired it. Pre-release, no remote, and the
only applied instance is a local SQLite DB where the constraint names are cosmetic.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_add_fx_rates"
down_revision: str | Sequence[str] | None = "0014_add_benchmarks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fx_rates",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column(
            "from_currency",
            sa.Enum(
                "INR",
                "USD",
                name="from_currency",
                native_enum=False,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "to_currency",
            sa.Enum(
                "INR",
                "USD",
                name="to_currency",
                native_enum=False,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("rate", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("rate > 0", name="rate_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_fx_rates"),
    )
    # One rate per (pair, date); shaped as the carry-forward read index.
    op.create_index(
        "uq_fx_rates_from_currency_to_currency_date",
        "fx_rates",
        ["from_currency", "to_currency", "date"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("fx_rates")
