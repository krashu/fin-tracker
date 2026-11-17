"""pre-link category: a link moves a row's category aside so unlink can restore it

Revision ID: 0044_pre_link_category
Revises: 0043_realised_lot_kind
Create Date: 2026-11-16

**A38** (ADR-0002 rule 9, amended). A link, auto F4a or manual, turns two existing rows into a
transfer pair, and a pair belongs to no category. The link therefore clears ``category_id`` on
both legs and stores it in the new nullable ``pre_link_category_id`` (a composite same-user FK
to ``categories``). Unlink and partner-delete restore it unless the user set a category on the
leg since: every category write on a linked leg clears the saved value, and CHECK
``pre_link_category_while_linked`` holds the rule in the DB (a saved category exists only on a
paired row whose own category is NULL).

The column is added NULL everywhere, so no preflight is needed and the CHECK holds. Rows linked
before this revision keep their category; there is no backfill (dev data only).

**Downgrade** folds each saved category back onto its row. The UPDATE must satisfy the CHECK,
which is still on the table at that point, so it sets ``category_id`` and clears
``pre_link_category_id`` in the same statement. Then the CHECK, the FK and the column go.

**SQLite batch hazard** (same as 0029 / 0037 / 0038 / 0041): the partial index
``ix_transactions_user_confirmed_date`` loses its predicate in a batch copy, so it is dropped
before and recreated after, in both directions.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0044_pre_link_category"
down_revision: str | Sequence[str] | None = "0043_realised_lot_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONFIRMED = "confirmed_at IS NOT NULL"
_PRE_LINK_CATEGORY_WHILE_LINKED = (
    "pre_link_category_id IS NULL OR (transfer_pair_id IS NOT NULL AND category_id IS NULL)"
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
    _drop_partial_index()
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.add_column(sa.Column("pre_link_category_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_transactions_pre_link_category_id_categories",
            "categories",
            ["pre_link_category_id", "user_id"],
            ["id", "user_id"],
        )
        batch_op.create_check_constraint(
            "pre_link_category_while_linked", _PRE_LINK_CATEGORY_WHILE_LINKED
        )
    _create_partial_index()


def downgrade() -> None:
    # One UPDATE: the CHECK is still enforced, so the saved value must clear as it moves.
    op.execute(
        "UPDATE transactions SET category_id = pre_link_category_id, "
        "pre_link_category_id = NULL WHERE pre_link_category_id IS NOT NULL"
    )
    _drop_partial_index()
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_constraint("pre_link_category_while_linked", type_="check")
        batch_op.drop_constraint(
            "fk_transactions_pre_link_category_id_categories", type_="foreignkey"
        )
        batch_op.drop_column("pre_link_category_id")
    _create_partial_index()
