"""rename investment_transactions.switch_pair_id to pair_id

Revision ID: 0026_rename_switch_pair_id_to_pair_id
Revises: 0025_fingerprint_separator_and_occurrence
Create Date: 2026-07-30

The column links the two legs of one economic event. It was named for the only
producer that ever existed — the CAS scheme-switch importer, removed in a00939a —
and from this revision its only live content is IDCW dividend-reinvestment pairs
(``POST /investment-transactions/reinvestment``), so ``switch_pair_id`` became an
active lie. Renamed to the neutral ``pair_id``; the pair's *kind* is read off its
member types (``(dividend, buy)`` reinvestment, ``(switch_out, switch_in)`` switch),
with no discriminator column — a nullable enum carrying one possible value would be
the speculative knob CLAUDE.md §2 forbids.

**No backfill: the column has never held a non-null value.** Every writer set
literal ``None`` (the create route, the CSV importer, the demo seeder) and the only
other write was the delete-time null-out. Data-inert rename.

Deliberately a raw ``op.execute("ALTER TABLE ... RENAME COLUMN ...")`` and **not**
``op.batch_alter_table`` — the same reasoning as 0019, which renamed on this exact
table. ``alembic/env.py`` turns on ``render_as_batch`` globally for SQLite, and a
batch op DROP+recreates the table; native ``RENAME COLUMN`` is metadata-only and
skips the rebuild entirely. SQLite ≥ 3.25 (guaranteed by Python 3.13's bundled
``sqlite3``) rewrites the FK clause, the CHECK clause, and any index definitions
that reference the column automatically, and Postgres does the same.

The two constraint NAMES deliberately keep their ``switch_pair`` spelling —
``fk_investment_transactions_switch_pair_same_user`` and
``ck_investment_transactions_no_self_pair``. Renaming a constraint on SQLite needs
the table rebuild this migration exists to avoid, and cosmetics are not worth the
riskiest migration shape in the tree (the hazard
``test_cli_upgrade_with_investment_data_succeeds`` exists to catch). The lag is
documented on the model.

``test_migration_parity`` stays green: ``_snapshot`` compares FKs by
``(constrained_columns, referred_table, referred_columns)`` with **no names**,
index names are compared but none change, uniques are column tuples only, and the
per-table CHECK count is unchanged because the CHECK is rewritten, not dropped.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0026_rename_switch_pair_id_to_pair_id"
down_revision: str | Sequence[str] | None = "0025_fingerprint_separator_and_occurrence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE investment_transactions RENAME COLUMN switch_pair_id TO pair_id")


def downgrade() -> None:
    op.execute("ALTER TABLE investment_transactions RENAME COLUMN pair_id TO switch_pair_id")
