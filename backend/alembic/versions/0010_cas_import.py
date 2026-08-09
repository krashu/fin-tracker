"""cas import: account-less batches + investment_transactions dedup columns

Revision ID: 0010_cas_import
Revises: 0009_add_transaction_auto_category_id
Create Date: 2026-06-18

Prepares the schema for account-less investment imports (PRD §F7). Originally added for
MF CAS import; the columns are now reused by the canonical CSV importer
(``investment_import_service``). The migration itself is unchanged:

1. ``import_batches.account_id`` → **nullable**. Spend-statement batches (PRD §F1)
   are scoped to one account; CAS batches are account-less (investments are
   decoupled from the spend tables). The ``source_file_hash`` re-upload short-circuit
   and the batch lifecycle are otherwise reused unchanged.
2. ``investment_transactions`` gains:
   * ``import_batch_id`` — reserved audit-trail FK to the originating batch (NULL for
     manual rows). No batch-cancel verb this slice; declared now like ``switch_pair_id``
     so a later slice needs no migration.
   * ``fingerprint`` (NULL for manual rows) + a UNIQUE index
     ``(user_id, instrument_id, fingerprint)`` backing idempotent re-import. Mirrors
     the spend side's ``uq_transactions_user_account_fingerprint`` but **nullable**:
     NULLs are distinct under UNIQUE on SQLite *and* Postgres, so the backstop is inert
     for manual rows by design (they aren't the dedup concern).

Hand-written so names match the SA NAMING_CONVENTION; ``tests/test_migration_parity.py``
guards drift on every pytest run (and a populated-upgrade-through-0010 regression test
covers the rebuild on real data — the empty in-memory parity DB cannot).

**Step order is load-bearing.** Do step 1 (``import_batches`` rebuild) before step 2,
so the FK target table is in its final shape before ``investment_transactions`` is
recreated referencing it.

**SQLite batch rebuild of a table carrying a self-referential composite FK.** Step 2
recreates ``investment_transactions``, which already carries the
``fk_investment_transactions_switch_pair_same_user`` composite FK → ``(id, user_id)``
(migration 0007). The batch copy ``INSERT INTO _alembic_tmp_... SELECT FROM
investment_transactions`` re-emits that FK; it validates **because** the composite-unique
target ``uq_investment_transactions_id_user`` already exists on the OLD (populated) table
(the migration-0005 analysis: ``foreign key mismatch`` is a *schema*-validation error
that ``PRAGMA foreign_keys=OFF`` does not suppress — the target index must pre-exist).
Re-emitting a self-ref composite FK inside a batch rebuild on a populated DB is itself
proven by migration 0009 (which rebuilds ``transactions``, carrying the analogous 0005
composite FK) under ``test_cli_upgrade_with_referencing_data_succeeds``. CLI migrations
run with ``PRAGMA foreign_keys=OFF`` (see ``alembic/env.py``) so the rebuild's implicit
row-delete on a referenced table doesn't trip a child FK on a populated DB.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_cas_import"
down_revision: str | Sequence[str] | None = "0009_add_transaction_auto_category_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Step 1 — import_batches.account_id → nullable (CAS batches are account-less).
    with op.batch_alter_table("import_batches") as batch_op:
        batch_op.alter_column("account_id", existing_type=sa.Integer(), nullable=True)

    # Step 2 — investment_transactions dedup + audit columns. import_batch_id is a
    # FK, so SQLite needs the batch rebuild (the self-ref composite FK re-validates
    # against the still-present uq_investment_transactions_id_user — see docstring).
    with op.batch_alter_table("investment_transactions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "import_batch_id",
                sa.Integer(),
                sa.ForeignKey(
                    "import_batches.id",
                    name="fk_investment_transactions_import_batch_id_import_batches",
                ),
                nullable=True,
            )
        )
        batch_op.add_column(sa.Column("fingerprint", sa.String(length=64), nullable=True))

    # Standalone after the rebuild: the column now exists, so the unique index is a
    # plain CREATE UNIQUE INDEX (no second rebuild). NULL fingerprints stay distinct.
    op.create_index(
        "uq_investment_transactions_user_instrument_fingerprint",
        "investment_transactions",
        ["user_id", "instrument_id", "fingerprint"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_investment_transactions_user_instrument_fingerprint",
        table_name="investment_transactions",
    )
    with op.batch_alter_table("investment_transactions") as batch_op:
        batch_op.drop_column("fingerprint")
        batch_op.drop_column("import_batch_id")
    # Restores NOT NULL; fails loudly if any account-less (CAS) batch exists, which
    # is correct — a downgrade past CAS support must not silently orphan those rows.
    with op.batch_alter_table("import_batches") as batch_op:
        batch_op.alter_column("account_id", existing_type=sa.Integer(), nullable=False)
