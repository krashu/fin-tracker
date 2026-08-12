"""collapse refund out of transactions.transaction_type

Revision ID: 0029_refund_as_signed_spend
Revises: 0028_add_origin_fingerprint
Create Date: 2026-08-10

Narrows ``transaction_type`` from ``spend|income|transfer|refund`` to
``spend|income|transfer`` (``docs/adr/0009-refund-as-signed-spend.md``). A refund
becomes a ``spend`` row carrying a *positive* ``amount_paise`` — derived at read
time, never stored.

Existing ``refund`` rows already store a positive amount (the F2/§F4a sign rule
and ``RawTransaction.__post_init__`` both enforced ``refund >= 0``), so the data
step is a pure retype: no amount is rewritten.

**Identity is untouched.** The ADR-0006 fingerprint payload is
``date ⋅ amount_paise ⋅ merchant_normalized ⋅ account_id`` — ``transaction_type``
was never hashed. No fingerprint, ``origin_fingerprint`` or ``occurrence`` moves,
so no ADR-0006 §Recompute procedure is triggered.

**SQLite batch-rebuild notes.** Re-cutting the enum CHECK rebuilds ``transactions``,
which drags in two hazards this file has to step around:

1. ``ix_transactions_user_confirmed_date`` is a PARTIAL index
   (``WHERE confirmed_at IS NOT NULL``). SQLite index reflection drops the
   predicate during a batch copy, so the index must be dropped before the batch
   and recreated after it — in BOTH directions, since the downgrade rebuilds the
   same table. 0008 hit exactly this (see its steps 1 / 3-5). Nothing else would
   catch a silent loss: ``test_migration_parity`` does not compare partial-index
   WHERE clauses (``app/models/transaction.py`` says so at the Index declaration),
   which is why ``tests/test_migration_parity.py`` asserts the predicate
   explicitly for this revision.
2. The self-referential composite FK ``fk_transactions_transfer_pair_same_user``
   is 0005's ``foreign key mismatch`` trap. It is already neutralised here:
   ``uq_transactions_id_user`` exists on the old table from 0005, giving the copy
   step a valid composite-unique target. That index must therefore NOT be dropped.

The CHECK is Enum-generated (``create_constraint=True``, ``name="transaction_type"``),
so the bare token is passed to batch and the ``ck`` naming convention in
``app/models/base.py`` expands it to ``ck_transactions_transaction_type`` — same
idiom as 0008, and unlike 0005's hand-named ``ck_transactions_no_self_pair``.

**The downgrade is reconstructive, not a byte-exact inverse.** It re-types every
positive ``spend`` back to ``refund``. That is exactly right for rows this
migration converted, but a *legitimately* positive non-refund spend — a state only
reachable after this revision, since ``spend > 0`` was rejected before it — would
come back typed ``refund``. Deterministic and lossless in aggregate (the sign,
which is what every F8 aggregate reads, is untouched either way).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0029_refund_as_signed_spend"
down_revision: str | Sequence[str] | None = "0028_add_origin_fingerprint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PARTIAL_INDEX = "ix_transactions_user_confirmed_date"
_PARTIAL_WHERE = "confirmed_at IS NOT NULL"


def _drop_partial_index() -> None:
    op.drop_index(_PARTIAL_INDEX, table_name="transactions")


def _create_partial_index() -> None:
    op.create_index(
        _PARTIAL_INDEX,
        "transactions",
        ["user_id", "date", "id"],
        sqlite_where=sa.text(_PARTIAL_WHERE),
        postgresql_where=sa.text(_PARTIAL_WHERE),
    )


def upgrade() -> None:
    # 1. Retype first, so no surviving row violates the narrowed CHECK. Amounts
    #    are already positive on these rows — nothing is re-signed.
    op.execute(
        sa.text(
            "UPDATE transactions SET transaction_type = 'spend' WHERE transaction_type = 'refund'"
        )
    )

    # 2. Drop the partial index before the rebuild (see module docstring note 1).
    _drop_partial_index()

    # 3. Re-cut the enum CHECK. Bare token: batch re-applies the ck convention.
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_constraint("transaction_type", type_="check")
        batch_op.create_check_constraint(
            "transaction_type",
            "transaction_type IN ('spend', 'income', 'transfer')",
        )

    # 4. Recreate the partial index with its predicate intact.
    _create_partial_index()


def downgrade() -> None:
    # Reverse order: widen the CHECK first so the retype below has somewhere to
    # land. Same partial-index dance — the downgrade rebuilds the table too.
    _drop_partial_index()

    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_constraint("transaction_type", type_="check")
        batch_op.create_check_constraint(
            "transaction_type",
            "transaction_type IN ('spend', 'income', 'transfer', 'refund')",
        )

    _create_partial_index()

    # Reconstructive, not byte-exact — see the module docstring.
    op.execute(
        sa.text(
            "UPDATE transactions SET transaction_type = 'refund' "
            "WHERE transaction_type = 'spend' AND amount_paise > 0"
        )
    )
