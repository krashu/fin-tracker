"""adr-0002: composite same-user invariant on transactions.transfer_pair_id

Revision ID: 0005_adr0002_transfer_pair_constraints
Revises: 0004_add_transaction_confirmed_at
Create Date: 2026-05-28

Lands the DB-level invariants from ADR-0002
(``docs/adr/0002-transfer-pair-id-semantics.md``, flipped to Accepted at
this revision):

1. ``uq_transactions_id_user`` — composite UNIQUE INDEX on
   ``(id, user_id)``. Composite FK targets need a composite unique
   index on Postgres; SQLite would accept the PK, but the index is
   Postgres-portability insurance.
2. ``fk_transactions_transfer_pair_same_user`` — composite FK
   ``(transfer_pair_id, user_id) → (id, user_id)``. Replaces the
   single-column FK from migration 0001. Guarantees any non-null
   ``transfer_pair_id`` references a row owned by the same user.
3. ``ck_transactions_no_self_pair`` — CHECK
   ``transfer_pair_id IS NULL OR transfer_pair_id != id``. A row cannot
   pair with itself.

**SQLite batch-mode self-referential-FK trap.** A naive
``batch_alter_table`` that adds both the composite unique and the
composite FK at once trips ``foreign key mismatch`` on the
``INSERT INTO _alembic_tmp_transactions ... SELECT FROM transactions``
copy step: the new table's composite FK references ``transactions(id,
user_id)``, but at copy time the OLD ``transactions`` table still has
only a single-column PK and no composite unique target.
``PRAGMA foreign_keys=OFF`` cannot save us — SQLite docs:
``PRAGMA foreign_keys`` is a no-op inside an open transaction, and
Alembic wraps migrations in a transaction.

Workaround: create the composite unique INDEX *first* as a standalone
``CREATE UNIQUE INDEX`` (which does not need batch and works on the
existing table). Once the OLD ``transactions`` table has the composite
unique target, the batch FK swap copy step validates cleanly.

Both halves of the model declaration (the ``Index(..., unique=True)``
and the ``ForeignKeyConstraint``) live in
``app/models/transaction.py``; this migration mirrors that declaration
shape to satisfy ``test_migration_parity``.

No backfill needed: no row in v0.1 carries a non-null
``transfer_pair_id`` (no writer exists yet — see ADR-0002 §Population).
The new constraints are no-ops at upgrade time and only fire on future
writes.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_adr0002_transfer_pair_constraints"
down_revision: str | Sequence[str] | None = "0004_add_transaction_confirmed_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Step 1 — add the composite unique INDEX as a standalone op so the
    # OLD transactions table has a valid composite-unique target before
    # the batch FK swap. See module docstring for the self-ref FK trap
    # this sidesteps.
    op.create_index(
        "uq_transactions_id_user",
        "transactions",
        ["id", "user_id"],
        unique=True,
    )
    # Step 2 — swap the single-col FK for the composite FK, and add the
    # no-self-pair CHECK. Ordering inside batch_op doesn't affect the
    # emitted DDL — Alembic collects all batch ops into one CREATE
    # TABLE in the recreate.
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_constraint(
            "fk_transactions_transfer_pair_id_transactions",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_transactions_transfer_pair_same_user",
            "transactions",
            ["transfer_pair_id", "user_id"],
            ["id", "user_id"],
        )
        batch_op.create_check_constraint(
            "ck_transactions_no_self_pair",
            "transfer_pair_id IS NULL OR transfer_pair_id != id",
        )


def downgrade() -> None:
    # Reverse order: swap FK + drop CHECK in batch first (so the new
    # table has only the single-col FK back to transactions.id, which is
    # always valid — old PK target), THEN drop the now-unused composite
    # unique index.
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_constraint("ck_transactions_no_self_pair", type_="check")
        batch_op.drop_constraint(
            "fk_transactions_transfer_pair_same_user",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_transactions_transfer_pair_id_transactions",
            "transactions",
            ["transfer_pair_id"],
            ["id"],
        )
    op.drop_index("uq_transactions_id_user", table_name="transactions")
