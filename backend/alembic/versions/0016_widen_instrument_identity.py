"""instruments active-symbol uniqueness: widen to include currency

Revision ID: 0016_widen_instrument_identity
Revises: 0015_add_fx_rates
Create Date: 2026-06-25

Widens the instruments active-symbol partial unique index from ``(user_id, symbol)`` to
``(user_id, symbol, currency)`` so a cross-listed ticker can be held once in INR and once
in USD without colliding (the F7 USD/FX milestone — a same-symbol INR-vs-USD pair is two
distinct instruments, not a duplicate). Mirrors the model-side ``Index`` in
``app/models/instrument.py``; ``tests/test_migration_parity.py`` guards the name + columns
and the partial WHERE predicate.

A partial unique index is a standalone object, so a plain DROP INDEX / CREATE INDEX works on
SQLite without ``batch_alter_table`` — the ``currency`` column already exists (added in 0007),
so no table rebuild is needed (contrast 0008, which added a column in the same step).

**Downgrade is data-dependently irreversible.** Re-creating the 2-column ``(user_id, symbol)``
unique index *raises* if active rows share a symbol across INR and USD — exactly the state this
migration enables. This is a forward-only widening; a real downgrade with dual-currency symbols
present needs a manual merge/archive first. Dev-only here (downgrades run against the test SQLite
DB per CLAUDE.md), and the parity downgrade test exercises only non-colliding data.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_widen_instrument_identity"
down_revision: str | Sequence[str] | None = "0015_add_fx_rates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("uq_instruments_active_user_symbol", table_name="instruments")
    op.create_index(
        "uq_instruments_active_user_symbol_currency",
        "instruments",
        ["user_id", "symbol", "currency"],
        unique=True,
        sqlite_where=sa.text("archived_at IS NULL"),
        postgresql_where=sa.text("archived_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_instruments_active_user_symbol_currency", table_name="instruments")
    op.create_index(
        "uq_instruments_active_user_symbol",
        "instruments",
        ["user_id", "symbol"],
        unique=True,
        sqlite_where=sa.text("archived_at IS NULL"),
        postgresql_where=sa.text("archived_at IS NULL"),
    )
