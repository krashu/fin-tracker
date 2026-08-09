"""add transactions.confirmed_at + partial board index

Revision ID: 0004_add_transaction_confirmed_at
Revises: 0003_seed_default_categories
Create Date: 2026-05-24

Adds the per-row review/commit gate (PRD §F1 step 5; cross-ref the
``imports-review-flow`` memory note). Imported rows land with
``confirmed_at IS NULL`` and require an explicit
``POST /imports/{batch_id}/commit`` to surface on the board. Manual F2
rows are stamped ``confirmed_at = now()`` at create time by the route
(not a column ``server_default`` — the column stays nullable so genuinely
unconfirmed import rows are distinguishable from F2 rows).

The partial index ``ix_transactions_user_confirmed_date`` scopes the
board's typical "newest 50 confirmed rows" lookup. WHERE predicate
mirrors the same clause on ``app/models/transaction.py __table_args__``;
keep in sync — ``test_migration_parity`` only checks
``(name, columns, unique)``, NOT WHERE. A separate test in
``tests/test_migration_parity.py`` asserts the rendered DDL contains the
predicate (the mechanical drift catch).

BACKFILL: legacy rows pre-date the review gate so they're de-facto
already on the board. ``UPDATE`` sets ``confirmed_at = created_at`` so
each row lands with a defensible timestamp (rather than ``now()`` which
would bunch every legacy row at the migration moment). This conflates
"imported-at" with "confirmed-at" for legacy rows — any future
"time-to-confirm" analytics must exclude rows where
``confirmed_at = created_at`` exactly (the legacy-row signature) or rows
older than this migration's deploy date.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_add_transaction_confirmed_at"
down_revision: str | Sequence[str] | None = "0003_seed_default_categories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
    )
    # Partial index — WHERE predicate mirrors app/models/transaction.py
    # __table_args__. Keep in sync; test_migration_parity does NOT verify
    # partial-index WHERE clauses.
    op.create_index(
        "ix_transactions_user_confirmed_date",
        "transactions",
        ["user_id", "date", "id"],
        sqlite_where=sa.text("confirmed_at IS NOT NULL"),
        postgresql_where=sa.text("confirmed_at IS NOT NULL"),
    )
    op.execute(
        sa.text("UPDATE transactions SET confirmed_at = created_at WHERE confirmed_at IS NULL")
    )


def downgrade() -> None:
    op.drop_index("ix_transactions_user_confirmed_date", table_name="transactions")
    op.drop_column("transactions", "confirmed_at")
