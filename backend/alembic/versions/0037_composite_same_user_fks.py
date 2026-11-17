"""composite same-user FKs on every owned→owned reference

Revision ID: 0037_composite_same_user_fks
Revises: 0036_add_guest_user_fields
Create Date: 2026-11-15

ADR-0003 rule 10: every owned→owned FK becomes ``(fk, user_id) → (id, user_id)``, so a row can
never point at another user's row even if the router's 422 check (rule 9) is skipped. ADR-0012
rule 1: ``categories.parent_id`` joins them and loses ``ON DELETE CASCADE`` (now NO ACTION), so a
hard parent delete fails while children exist; whole-user teardown deletes children first.

Ten FKs move; each keeps its current name, so nothing renames. Four tables gain the
``uq_<table>_id_user`` unique index the composite FKs reference (``transactions``,
``investment_transactions`` and ``labels`` already had theirs).

**Preflight.** On Postgres, adding the FK validates existing rows and fails on a cross-user link;
on SQLite (migrations run with ``foreign_keys=OFF``, ADR-0001 rule 4b) the batch copy would carry
such a row over silently and the next UPDATE of it would fail. So both dialects first count
cross-user links per FK and abort with table, column and count only — no ids, nothing personal.

**SQLite batch hazards** (same as 0005 / 0029):

1. The unique ``(id, user_id)`` targets are created as standalone indexes *before* any batch, so
   the self-referential FKs (``accounts.parent_account_id``, ``categories.parent_id``) find a valid
   composite target on the old table at copy time (0005's ``foreign key mismatch`` trap).
2. Partial indexes lose their predicate in a batch copy, so the three on rebuilt tables are
   dropped before and recreated after, in both directions.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0037_composite_same_user_fks"
down_revision: str | Sequence[str] | None = "0036_add_guest_user_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# New (id, user_id) reference targets.
_TARGETS = ("accounts", "categories", "import_batches", "instruments")

# (table, column, referred table, ON DELETE of the plain FK being replaced)
_FKS: tuple[tuple[str, str, str, str | None], ...] = (
    ("accounts", "parent_account_id", "accounts", None),
    ("categories", "parent_id", "categories", "CASCADE"),
    ("import_batches", "account_id", "accounts", None),
    ("investment_transactions", "instrument_id", "instruments", None),
    ("investment_transactions", "import_batch_id", "import_batches", None),
    ("merchant_tag_map", "category_id", "categories", None),
    ("transactions", "account_id", "accounts", None),
    ("transactions", "category_id", "categories", None),
    ("transactions", "auto_category_id", "categories", None),
    ("transactions", "import_batch_id", "import_batches", None),
)

# Partial indexes on tables the batch rebuilds: (name, table, columns, unique, predicate).
_PARTIAL_INDEXES: tuple[tuple[str, str, list[str], bool, str], ...] = (
    ("uq_accounts_active_user_name", "accounts", ["user_id", "name"], True, "archived_at IS NULL"),
    (
        "uq_categories_active_user_name",
        "categories",
        ["user_id", "name", "kind"],
        True,
        "archived_at IS NULL",
    ),
    (
        "ix_transactions_user_confirmed_date",
        "transactions",
        ["user_id", "date", "id"],
        False,
        "confirmed_at IS NOT NULL",
    ),
)


def _fk_name(table: str, column: str, referred: str) -> str:
    return f"fk_{table}_{column}_{referred}"


def _tables() -> list[str]:
    return list(dict.fromkeys(table for table, _, _, _ in _FKS))


def _preflight() -> None:
    bind = op.get_bind()
    offenders = []
    for table, column, referred, _ in _FKS:
        # Interpolated names are the module constants above, never input.
        count = bind.execute(
            sa.text(
                f"SELECT count(*) FROM {table} c JOIN {referred} p ON p.id = c.{column} "
                "WHERE p.user_id != c.user_id"
            )
        ).scalar_one()
        if count:
            offenders.append(
                f"{table}.{column}: {count} row(s) reference another user's {referred}"
            )
    if offenders:
        raise RuntimeError(
            "cross-user references block the composite same-user FKs (ADR-0003 rule 10); "
            "fix these rows first:\n" + "\n".join(offenders)
        )


def _drop_partial_indexes() -> None:
    for name, table, _, _, _ in _PARTIAL_INDEXES:
        op.drop_index(name, table_name=table)


def _create_partial_indexes() -> None:
    for name, table, columns, unique, where in _PARTIAL_INDEXES:
        op.create_index(
            name,
            table,
            columns,
            unique=unique,
            sqlite_where=sa.text(where),
            postgresql_where=sa.text(where),
        )


def upgrade() -> None:
    _preflight()
    for target in _TARGETS:
        op.create_index(f"uq_{target}_id_user", target, ["id", "user_id"], unique=True)
    _drop_partial_indexes()
    for table in _tables():
        with op.batch_alter_table(table) as batch_op:
            for fk_table, column, referred, _ in _FKS:
                if fk_table != table:
                    continue
                name = _fk_name(table, column, referred)
                batch_op.drop_constraint(name, type_="foreignkey")
                batch_op.create_foreign_key(name, referred, [column, "user_id"], ["id", "user_id"])
    _create_partial_indexes()


def downgrade() -> None:
    _drop_partial_indexes()
    for table in _tables():
        with op.batch_alter_table(table) as batch_op:
            for fk_table, column, referred, ondelete in _FKS:
                if fk_table != table:
                    continue
                name = _fk_name(table, column, referred)
                batch_op.drop_constraint(name, type_="foreignkey")
                batch_op.create_foreign_key(name, referred, [column], ["id"], ondelete=ondelete)
    _create_partial_indexes()
    for target in _TARGETS:
        op.drop_index(f"uq_{target}_id_user", table_name=target)
