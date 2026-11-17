"""every root category has a colour: CHECK root_has_color

Revision ID: 0046_root_category_color
Revises: 0045_asset_class_currency
Create Date: 2026-11-17

**A59** (ADR-0012 rule 7). A root category (``parent_id IS NULL``) always carries a colour; only a
subcategory may be ``NULL`` and inherit its parent's hue. The API, promotion and backup restore
assign the colour; this CHECK, ``ck_categories_root_has_color`` (ADR-0001 rules 3 and 3b), makes it
hold for every writer, including ones that bypass the API.

**No backfill.** A dev database holding a colourless root fails the preflight, by design. The
preflight is count-only, as 0037 and 0045 (no names, no ids), and the message says ``fix in SQL``.

**FK target.** ``categories`` is referenced by the transactions composite FKs,
``merchant_tag_map`` and its own parent FK; the batch rebuild keeps ``uq_categories_id_user`` and
is safe with ``foreign_keys=OFF`` (ADR-0001 rule 4b).

**SQLite batch hazard** (same as 0033 / 0045): the partial index ``uq_categories_active_user_name``
is dropped before the batch and recreated after, in both directions.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0046_root_category_color"
down_revision: str | Sequence[str] | None = "0045_asset_class_currency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen literals, never imported from the model.
_ROOT_HAS_COLOR = "parent_id IS NOT NULL OR color IS NOT NULL"
_ACTIVE = "archived_at IS NULL"


def _preflight() -> None:
    bind = op.get_bind()
    n = bind.execute(
        sa.text(f"SELECT count(*) FROM categories WHERE NOT ({_ROOT_HAS_COLOR})")
    ).scalar_one()
    if n:
        raise RuntimeError(
            "root category color CHECK (ADR-0012 rule 7) cannot be added; fix in SQL: set a "
            "'#rrggbb' color on each root category (parent_id IS NULL), or reset the dev "
            "database:\n"
            f"{n} root category row(s) have no color, which blocks CHECK root_has_color"
        )


def _drop_partial_index() -> None:
    op.drop_index("uq_categories_active_user_name", table_name="categories")


def _create_partial_index() -> None:
    op.create_index(
        "uq_categories_active_user_name",
        "categories",
        ["user_id", "name", "kind"],
        unique=True,
        sqlite_where=sa.text(_ACTIVE),
        postgresql_where=sa.text(_ACTIVE),
    )


def upgrade() -> None:
    _preflight()
    _drop_partial_index()
    with op.batch_alter_table("categories") as batch_op:
        batch_op.create_check_constraint("root_has_color", _ROOT_HAS_COLOR)
    _create_partial_index()


def downgrade() -> None:
    _drop_partial_index()
    with op.batch_alter_table("categories") as batch_op:
        batch_op.drop_constraint("root_has_color", type_="check")
    _create_partial_index()
