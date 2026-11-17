"""pairs, refund link and FX stamp: pair CHECKs, pre-link type, refund link, no FX default

Revision ID: 0038_pairs_refund_link_fx_stamp
Revises: 0037_composite_same_user_fks
Create Date: 2026-11-15

**C1.** The two ``no_self_pair`` CHECKs were named in full by hand, so the naming convention
prefixed them a second time (``ck_transactions_ck_transactions_no_self_pair``). ADR-0001 rule 3b:
a CHECK carries just its suffix. Both are dropped and recreated as ``no_self_pair``, which renders
``ck_<table>_no_self_pair``. The investment FK keeps its historical ``switch_pair`` name (ADR-0002
Consequences).

**A3.** CHECK ``paired_is_transfer`` (``transfer_pair_id IS NULL OR transaction_type =
'transfer'``, ADR-0002 rule 8): the type, not the link, excludes a row from spend and income.

**A16.** ``transactions.pre_link_type`` (nullable, the transaction-type vocabulary, CHECK
``pre_link_type``): the type an existing row had before a link made it a transfer (ADR-0002 rule
9). Added NULL everywhere: rows linked before this revision keep ``transfer`` on unlink, as they
did. The Enum's own CHECK is not emitted inside the batch (it would be emitted twice); it is
created by name instead, the 0008 idiom.

**A29.** ``transactions.refund_of_id`` (nullable): an optional refund → original link (ADR-0009
rule 13) behind the composite same-user self-FK ``fk_transactions_refund_of_id_transactions``
``(refund_of_id, user_id) → (id, user_id)``, ON DELETE NO ACTION, the 0037 shape. Added NULL, so
it needs no preflight.

**A34.** ``investment_transactions.fx_rate_to_inr`` loses its ``DEFAULT 1000000`` (ADR-0016:
server-stamped at the transaction date, never defaulted). Every writer already stamps it.

**Preflight.** On Postgres adding the CHECK validates existing rows; on SQLite the batch copy
would carry a violating row over silently and its next UPDATE would fail. So the upgrade first
counts paired rows that are not transfers and aborts with the count only — no ids, nothing a
user typed. There is no auto-fix: which type such a row should have is the user's call.

**Downgrade** is a faithful inverse of the schema but drops data the old schema cannot hold:
every stored pre-link type (those legs then stay ``transfer`` on unlink, as before) and every
refund link.

**SQLite batch hazard** (same as 0029 / 0037): the partial index
``ix_transactions_user_confirmed_date`` loses its predicate in a batch copy, so it is dropped
before and recreated after, in both directions.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0038_pairs_refund_link_fx_stamp"
down_revision: str | Sequence[str] | None = "0037_composite_same_user_fks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TXN_SELF_PAIR = "transfer_pair_id IS NULL OR transfer_pair_id != id"
_INV_SELF_PAIR = "pair_id IS NULL OR pair_id != id"
_CONFIRMED = "confirmed_at IS NOT NULL"
_PAIRED_IS_TRANSFER = "transfer_pair_id IS NULL OR transaction_type = 'transfer'"
_TXN_TYPES = ("spend", "income", "transfer")
_PRE_LINK_TYPE = "pre_link_type IN ('spend', 'income', 'transfer')"


def _preflight() -> None:
    count = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM transactions "
                "WHERE transfer_pair_id IS NOT NULL AND transaction_type != 'transfer'"
            )
        )
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            f"{count} paired transaction row(s) are not of type 'transfer', which blocks "
            "CHECK paired_is_transfer (ADR-0002 rule 8); unlink or retype them first"
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
    _preflight()
    _drop_partial_index()
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_transactions_ck_transactions_no_self_pair"), type_="check"
        )
        batch_op.create_check_constraint("no_self_pair", _TXN_SELF_PAIR)
        batch_op.create_check_constraint("paired_is_transfer", _PAIRED_IS_TRANSFER)
        batch_op.add_column(
            sa.Column(
                "pre_link_type",
                sa.Enum(
                    *_TXN_TYPES, name="pre_link_type", native_enum=False, create_constraint=False
                ),
                nullable=True,
            )
        )
        batch_op.create_check_constraint("pre_link_type", _PRE_LINK_TYPE)
        batch_op.add_column(sa.Column("refund_of_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_transactions_refund_of_id_transactions",
            "transactions",
            ["refund_of_id", "user_id"],
            ["id", "user_id"],
        )
    _create_partial_index()

    with op.batch_alter_table("investment_transactions") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_investment_transactions_ck_investment_transactions_no_self_pair"),
            type_="check",
        )
        batch_op.create_check_constraint("no_self_pair", _INV_SELF_PAIR)
        batch_op.alter_column(
            "fx_rate_to_inr",
            existing_type=sa.BigInteger(),
            existing_nullable=False,
            server_default=None,
        )


def downgrade() -> None:
    with op.batch_alter_table("investment_transactions") as batch_op:
        batch_op.alter_column(
            "fx_rate_to_inr",
            existing_type=sa.BigInteger(),
            existing_nullable=False,
            server_default=sa.text("1000000"),
        )
        batch_op.drop_constraint("no_self_pair", type_="check")
        batch_op.create_check_constraint(
            op.f("ck_investment_transactions_ck_investment_transactions_no_self_pair"),
            _INV_SELF_PAIR,
        )

    _drop_partial_index()
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_constraint("fk_transactions_refund_of_id_transactions", type_="foreignkey")
        batch_op.drop_column("refund_of_id")
        batch_op.drop_constraint("pre_link_type", type_="check")
        batch_op.drop_column("pre_link_type")
        batch_op.drop_constraint("paired_is_transfer", type_="check")
        batch_op.drop_constraint("no_self_pair", type_="check")
        batch_op.create_check_constraint(
            op.f("ck_transactions_ck_transactions_no_self_pair"), _TXN_SELF_PAIR
        )
    _create_partial_index()
