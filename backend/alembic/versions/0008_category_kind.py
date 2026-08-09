"""category kind: spend vs income scope

Revision ID: 0008_category_kind
Revises: 0007_add_investment_tables
Create Date: 2026-06-18

Adds a ``kind`` (spend|income) scope to categories so income gets its own
category set (Salary, Freelancing, Cashback, Other) parallel to spend
(Food, Transport, ...). Mirrors the model-side declaration in
``app/models/category.py``.

Upgrade order is load-bearing — add column, swap the active-name unique
index to include ``kind``, seed income rows, then archive the vestigial
flat seeds:

1. ADD COLUMN ``kind`` NOT NULL DEFAULT 'spend' — backfills all 15 existing
   seeds (and any user rows) to the only legacy scope, spend.
2. Swap ``uq_categories_active_user_name`` from ``(user_id, name)`` to
   ``(user_id, name, kind)``. A partial unique index is a standalone object,
   so a plain DROP INDEX / CREATE INDEX works on SQLite without
   ``batch_alter_table`` (contrast 0005, which needed batch for a
   table-level FK/CHECK swap). The 3rd column lets income "Other" coexist
   with the existing spend "Other".
3. Seed the 4 income categories (kind='income', is_seeded=True). The
   ``sa.table`` MUST include ``kind`` or the rows fall through to the
   ``spend`` server_default.
4. Archive the vestigial flat "Income"/"Transfer" seeds: the typed income
   categories supersede flat "Income"; "Transfer" was never used (transfers
   carry a null category and are excluded from dashboards). Archiving (not
   deleting/converting) preserves any rows already pointing at them and is
   cleanly reversible.

Reversible. WARNING: 0008's downgrade with income-tagged transactions
present leaves ``transactions.category_id`` dangling at a deleted income
row (same class of risk as 0003's downgrade; dev-only). The downgrade
rebuilds the table via ``batch_alter_table`` to drop ``kind`` because
SQLite refuses ``DROP COLUMN`` on a column named in a CHECK constraint
(the ``category_kind`` enum CHECK). Parameterised ``sa.text`` is used so
the v1-user UUID matches on both SQLite (hex-without-dashes) and Postgres
(native uuid).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0008_category_kind"
down_revision: str | Sequence[str] | None = "0007_add_investment_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_V1_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_INCOME_NAMES: tuple[str, ...] = ("Salary", "Freelancing", "Cashback", "Other")


def upgrade() -> None:
    # 1. Drop the 2-col partial index FIRST so the batch table-rebuild below
    #    doesn't reflect-and-mangle its partial WHERE clause (SQLite index
    #    reflection drops the predicate). Recreated as 3-col after the add.
    op.drop_index("uq_categories_active_user_name", table_name="categories")

    # 2. Add kind + its CHECK via batch_alter_table. Two SQLite gotchas drive
    #    this shape: (a) a plain ALTER ADD COLUMN silently skips an enum's
    #    implicit CHECK ("Skipping unsupported ALTER..."), and (b) letting the
    #    enum auto-create the CHECK *inside* batch emits it twice (raw
    #    `category_kind` + convention `ck_categories_category_kind`). So add the
    #    column with create_constraint=False and add exactly one explicit named
    #    CHECK — matching the model's single create_constraint=True CHECK for
    #    the parity CHECK-count test. server_default 'spend' backfills existing
    #    rows during the table copy; the model also carries a Python-side
    #    default for ORM inserts that omit kind.
    with op.batch_alter_table("categories") as batch_op:
        batch_op.add_column(
            sa.Column(
                "kind",
                sa.Enum(
                    "spend",
                    "income",
                    name="category_kind",
                    native_enum=False,
                    create_constraint=False,
                    validate_strings=True,
                ),
                nullable=False,
                server_default="spend",
            )
        )
        batch_op.create_check_constraint("category_kind", "kind IN ('spend', 'income')")

    # 3. Recreate the active-name unique index, now 3-col, so income "Other"
    #    can coexist with the existing spend "Other".
    op.create_index(
        "uq_categories_active_user_name",
        "categories",
        ["user_id", "name", "kind"],
        unique=True,
        sqlite_where=sa.text("archived_at IS NULL"),
        postgresql_where=sa.text("archived_at IS NULL"),
    )

    # 4. Seed the income categories. sa.table MUST carry kind (0003's does not)
    #    or these fall through to the spend server_default.
    categories_table = sa.table(
        "categories",
        sa.column("user_id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("kind", sa.String()),
        sa.column("is_seeded", sa.Boolean()),
    )
    op.bulk_insert(
        categories_table,
        [
            {"user_id": _V1_USER_ID, "name": n, "kind": "income", "is_seeded": True}
            for n in _INCOME_NAMES
        ],
    )

    # 5. Archive the vestigial flat spend seeds the typed model supersedes.
    op.execute(
        sa.text(
            "UPDATE categories SET archived_at = :ts "
            "WHERE is_seeded = TRUE AND user_id = :uid AND kind = 'spend' "
            "AND name IN ('Income', 'Transfer') AND archived_at IS NULL"
        ).bindparams(ts=datetime.now(UTC), uid=_V1_USER_ID)
    )


def downgrade() -> None:
    # Reverse order. 1. Un-archive only the flats 0008 archived (seeded, v1
    #    user, spend kind, those two names).
    op.execute(
        sa.text(
            "UPDATE categories SET archived_at = NULL "
            "WHERE is_seeded = TRUE AND user_id = :uid AND kind = 'spend' "
            "AND name IN ('Income', 'Transfer')"
        ).bindparams(uid=_V1_USER_ID)
    )
    # 2. Delete the income seeds.
    op.execute(
        sa.text(
            "DELETE FROM categories WHERE is_seeded = TRUE AND user_id = :uid AND kind = 'income'"
        ).bindparams(uid=_V1_USER_ID)
    )
    # 3. Drop the 3-col index before the table rebuild — it references the
    #    column being dropped, and its partial WHERE doesn't survive batch
    #    reflection cleanly.
    op.drop_index("uq_categories_active_user_name", table_name="categories")
    # 4. Rebuild the table without kind. SQLite can't DROP a column named in a
    #    CHECK, so batch-recreate; drop the enum CHECK explicitly because
    #    SQLite CHECK reflection doesn't associate it with the column. Pass the
    #    bare token ("category_kind") — batch re-applies the ck naming
    #    convention, expanding it to ck_categories_category_kind.
    with op.batch_alter_table("categories") as batch_op:
        batch_op.drop_constraint("category_kind", type_="check")
        batch_op.drop_column("kind")
    # 5. Restore the original 2-col active-name unique index.
    op.create_index(
        "uq_categories_active_user_name",
        "categories",
        ["user_id", "name"],
        unique=True,
        sqlite_where=sa.text("archived_at IS NULL"),
        postgresql_where=sa.text("archived_at IS NULL"),
    )
