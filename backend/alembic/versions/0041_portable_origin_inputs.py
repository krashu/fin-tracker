"""portable origin inputs: freeze the pre-edit identity next to origin_fingerprint

Revision ID: 0041_portable_origin_inputs
Revises: 0040_add_realised_lots
Create Date: 2026-11-15

**A14** (ADR-0006 rule 17). ``origin_fingerprint`` is a hash that bakes in the install-local
``account_id``, so a backup cannot carry it. The first identity edit of an imported row now
freezes the inputs the hash was made from in four nullable columns, ``origin_date``,
``origin_amount_paise``, ``origin_merchant_normalized`` and ``origin_account_id`` (a composite
same-user FK; an account move is an identity edit, so the origin account must be stored), behind
CHECK ``origin_inputs_all_or_none``: all four or none, and set inputs imply a stored origin hash.
Backup export carries the inputs, never a hash; restore recomputes both hashes on the target.

Invariant afterwards: ``origin_fingerprint`` NULL means no provenance; origin inputs NULL means
``origin_fingerprint == fingerprint``; inputs set means ``origin_fingerprint == H(inputs)``.

**Upgrade is one-way for rows edited before this revision** (ADR-0006 rule 21). Their
``origin_fingerprint`` differs from ``fingerprint`` and their inputs cannot be recovered from a
hash, so the hash is NULLed: keeping it would break the invariant (the exporter would write the
current identity as the origin, and the next edit would freeze the wrong inputs next to a
mismatched hash). The cost is that such a row may re-stage one statement line once. Only dev
data is affected.

**Downgrade** drops the four columns, the FK and the CHECK. ``origin_fingerprint`` already holds
the correct hash for every edited row, so the old schema's meaning is intact; the hashes nulled on
upgrade stay NULL.

**SQLite batch hazard** (same as 0029 / 0037 / 0038): the partial index
``ix_transactions_user_confirmed_date`` loses its predicate in a batch copy, so it is dropped
before and recreated after, in both directions.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0041_portable_origin_inputs"
down_revision: str | Sequence[str] | None = "0040_add_realised_lots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONFIRMED = "confirmed_at IS NOT NULL"
_ORIGIN_ALL_OR_NONE = (
    "(origin_date IS NULL AND origin_amount_paise IS NULL "
    "AND origin_merchant_normalized IS NULL AND origin_account_id IS NULL) "
    "OR (origin_date IS NOT NULL AND origin_amount_paise IS NOT NULL "
    "AND origin_merchant_normalized IS NOT NULL AND origin_account_id IS NOT NULL "
    "AND origin_fingerprint IS NOT NULL)"
)


def _drop_partial_index() -> None:
    op.drop_index("ix_transactions_user_confirmed_date", table_name="transactions")


def _create_partial_index() -> None:
    op.create_index(
        "ix_transactions_user_confirmed_date",
        "transactions",
        ["user_id", "date", "id"],
        sqlite_where=sa.text(_CONFIRMED),
        postgresql_where=sa.text(_CONFIRMED),
    )


def upgrade() -> None:
    # One-way (see the docstring): an edited row's origin inputs are unrecoverable.
    op.execute(
        "UPDATE transactions SET origin_fingerprint = NULL "
        "WHERE origin_fingerprint IS NOT NULL AND origin_fingerprint != fingerprint"
    )
    _drop_partial_index()
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.add_column(sa.Column("origin_date", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("origin_amount_paise", sa.BigInteger(), nullable=True))
        batch_op.add_column(
            sa.Column("origin_merchant_normalized", sa.String(length=512), nullable=True)
        )
        batch_op.add_column(sa.Column("origin_account_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_transactions_origin_account_id_accounts",
            "accounts",
            ["origin_account_id", "user_id"],
            ["id", "user_id"],
        )
        batch_op.create_check_constraint("origin_inputs_all_or_none", _ORIGIN_ALL_OR_NONE)
    _create_partial_index()


def downgrade() -> None:
    _drop_partial_index()
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_constraint("origin_inputs_all_or_none", type_="check")
        batch_op.drop_constraint("fk_transactions_origin_account_id_accounts", type_="foreignkey")
        batch_op.drop_column("origin_account_id")
        batch_op.drop_column("origin_merchant_normalized")
        batch_op.drop_column("origin_amount_paise")
        batch_op.drop_column("origin_date")
    _create_partial_index()
