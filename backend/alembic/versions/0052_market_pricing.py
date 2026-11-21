"""add market pricing tables (shared reference NAV cache and jobs)

Revision ID: 0052_market_pricing
Revises: 0051_spend_seam_and_investment_transfer
Create Date: 2026-11-21

Milestone 2 Phase 3 (ADR-0021):
1. Creates normalized, clustered market pricing tables:
   - market_instruments (universe/catalog)
   - market_price_history (raw daily prices, WITHOUT ROWID)
   - market_price_coverage (per-instrument coverage & watermarks)
   - market_corporate_actions (split adjustments)
   - market_instrument_pins (pinned benchmarks/FX)
   - market_price_jobs (persistent fetch/backfill queue)
   - market_host_state (circuit breaker state)
   - market_task_runs (maintenance/refresh run records)
2. Global reference data without user_id (ADR-0003 exception).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0052_market_pricing"
down_revision: str | Sequence[str] | None = "0051_spend_seam_and_investment_transfer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    nav_date_type = sa.Integer() if is_sqlite else sa.Date()

    op.create_table(
        "market_instruments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("price_key", sa.String(length=64), nullable=False),
        sa.Column("asset_class", sa.String(length=12), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("provider_symbol", sa.String(length=64), nullable=True),
        sa.Column("upstream_dead", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("unreferenced_since", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_market_instruments"),
        sa.UniqueConstraint("price_key", name="uq_market_instruments_price_key"),
    )

    history_kwargs: dict[str, object] = {}
    if is_sqlite:
        history_kwargs["sqlite_with_rowid"] = False

    op.create_table(
        "market_price_history",
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("nav_date", nav_date_type, nullable=False),
        sa.Column("nav_micros", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("instrument_id", "nav_date", name="pk_market_price_history"),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["market_instruments.id"],
            name="fk_market_price_history_instrument_id_market_instruments",
            ondelete="CASCADE",
        ),
        **history_kwargs,
    )

    op.create_table(
        "market_price_coverage",
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("covered_from", sa.Date(), nullable=True),
        sa.Column("covered_to", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="pending"),
        sa.Column("inception_reached", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column(
            "consecutive_failures", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("instrument_id", name="pk_market_price_coverage"),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["market_instruments.id"],
            name="fk_market_price_coverage_instrument_id_market_instruments",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "market_corporate_actions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("action_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False, server_default="split"),
        sa.Column("numerator", sa.Float(), nullable=False),
        sa.Column("denominator", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_market_corporate_actions"),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["market_instruments.id"],
            name="fk_market_corporate_actions_instrument_id_market_instruments",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("instrument_id", "action_date", "kind", name="uq_market_corp_action"),
    )

    op.create_table(
        "market_instrument_pins",
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("instrument_id", "reason", name="pk_market_instrument_pins"),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["market_instruments.id"],
            name="fk_market_instrument_pins_instrument_id_market_instruments",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "market_price_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False, server_default="backfill"),
        sa.Column("host", sa.String(length=24), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("not_before", sa.DateTime(), nullable=False),
        sa.Column("range_start", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_market_price_jobs"),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["market_instruments.id"],
            name="fk_market_price_jobs_instrument_id_market_instruments",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "uq_market_price_jobs_active",
        "market_price_jobs",
        ["instrument_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('queued', 'running')"),
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_market_price_jobs_claim",
        "market_price_jobs",
        ["status", "priority", "id"],
    )

    op.create_table(
        "market_host_state",
        sa.Column("host", sa.String(length=24), nullable=False),
        sa.Column("paused_until", sa.DateTime(), nullable=True),
        sa.Column("trips", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("host", name="pk_market_host_state"),
    )

    op.create_table(
        "market_task_runs",
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("last_run_at", sa.DateTime(), nullable=False),
        sa.Column("last_status", sa.String(length=12), nullable=False, server_default="ok"),
        sa.Column("last_detail", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("name", name="pk_market_task_runs"),
    )


def downgrade() -> None:
    op.drop_table("market_task_runs")
    op.drop_table("market_host_state")
    op.drop_index("ix_market_price_jobs_claim", table_name="market_price_jobs")
    op.drop_index("uq_market_price_jobs_active", table_name="market_price_jobs")
    op.drop_table("market_price_jobs")
    op.drop_table("market_instrument_pins")
    op.drop_table("market_corporate_actions")
    op.drop_table("market_price_coverage")
    op.drop_table("market_price_history")
    op.drop_table("market_instruments")
