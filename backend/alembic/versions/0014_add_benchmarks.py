"""add benchmark tables (benchmarks, benchmark_nav) + seed the index-fund catalog

Revision ID: 0014_add_benchmarks
Revises: 0013_add_instrument_identifiers
Create Date: 2026-06-21

Lands the F8-view-5 benchmark data model (PRD §Data model): ``benchmarks`` (curated
INR index *funds*, global reference data — no ``user_id``) and ``benchmark_nav`` (each
fund's daily NAV history, a price cache). Both hand-written so constraint / index names
match the SA NAMING_CONVENTION; ``tests/test_migration_parity.py`` guards drift.

**Scaled-int storage.** ``benchmark_nav.nav`` is an exact decimal stored as a scaled
``int64`` via the ``PriceNative`` TypeDecorator (scale 1e8) — but a TypeDecorator over
``BigInteger`` introspects as ``BIGINT``, so the storage shape here is plain
``sa.BigInteger()`` (the decorator is Python-semantics only), mirroring 0007.

**Catalog seed.** The 7 reference rows are seeded here (not in the seed script): they are
schema-coupled global reference data every install needs for the benchmark picker, exactly
like the 0003/0008 category seeds. The bulky NAV *history* is NOT seeded here — it is
backfilled by ``benchmark_service.refresh_benchmark_navs`` (mfapi), invoked at seed time.
``name`` is the index name (display label wraps it "post-expense TRI NAV, not the raw
index"); ``amfi_code`` is the direct-growth scheme code (mfapi keys on the same code).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_add_benchmarks"
down_revision: str | Sequence[str] | None = "0013_add_instrument_identifiers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Curated INR index funds, keyed by AMFI direct-growth scheme code (mfapi uses the same
# code). `name` is the index; the UI labels it as a post-expense fund, not the raw index.
_CATALOG: list[dict[str, str]] = [
    {"name": "Nifty 50", "kind": "index_fund", "amfi_code": "120716", "currency": "INR"},
    {"name": "Nifty Next 50", "kind": "index_fund", "amfi_code": "143341", "currency": "INR"},
    {"name": "Nifty Midcap 150", "kind": "index_fund", "amfi_code": "147622", "currency": "INR"},
    {"name": "Nifty 500", "kind": "index_fund", "amfi_code": "147625", "currency": "INR"},
    {"name": "Nifty Bank", "kind": "index_fund", "amfi_code": "147620", "currency": "INR"},
    {"name": "Nasdaq 100", "kind": "index_fund", "amfi_code": "145552", "currency": "INR"},
    {"name": "S&P 500", "kind": "index_fund", "amfi_code": "148381", "currency": "INR"},
]


def upgrade() -> None:
    # ------------------------------------------------------------- benchmarks
    op.create_table(
        "benchmarks",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "index_fund",
                name="benchmark_kind",
                native_enum=False,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=False,
            server_default=sa.text("'index_fund'"),
        ),
        sa.Column("amfi_code", sa.String(length=16), nullable=False),
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
        sa.Column("inception_date", sa.Date(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_benchmarks"),
    )

    # ------------------------------------------------------------ benchmark_nav
    op.create_table(
        "benchmark_nav",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("benchmark_id", sa.Integer(), nullable=False),
        sa.Column("nav_date", sa.Date(), nullable=False),
        sa.Column("nav", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["benchmark_id"],
            ["benchmarks.id"],
            name="fk_benchmark_nav_benchmark_id_benchmarks",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_benchmark_nav"),
    )
    # Dedup + the forward-pricing read index (one NAV per fund per day).
    op.create_index(
        "uq_benchmark_nav_benchmark_id_nav_date",
        "benchmark_nav",
        ["benchmark_id", "nav_date"],
        unique=True,
    )

    # Seed the catalog. Bulk insert fires the server_defaults for created_at/updated_at;
    # inception_date / archived_at stay NULL.
    benchmarks_tbl = sa.table(
        "benchmarks",
        sa.column("name", sa.String),
        sa.column("kind", sa.String),
        sa.column("amfi_code", sa.String),
        sa.column("currency", sa.String),
    )
    op.bulk_insert(benchmarks_tbl, _CATALOG)


def downgrade() -> None:
    # Reverse dependency order: benchmark_nav FKs benchmarks.
    op.drop_table("benchmark_nav")
    op.drop_table("benchmarks")
