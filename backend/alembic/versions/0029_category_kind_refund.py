"""category kind: add refund to spend vs income scope

Revision ID: 0029_category_kind_refund
Revises: 0028_add_origin_fingerprint
Create Date: 2026-08-09

Widens category_kind CHECK constraint to include 'refund' alongside
'spend' and 'income'.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_category_kind_refund"
down_revision: str | Sequence[str] | None = "0028_add_origin_fingerprint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("categories") as batch_op:
        batch_op.drop_constraint("category_kind", type_="check")
        batch_op.create_check_constraint(
            "category_kind", "kind IN ('spend', 'income', 'refund')"
        )

    bind = op.get_bind()
    users = bind.execute(sa.text("SELECT id FROM users")).fetchall()
    for row in users:
        user_id = row[0]
        has_refund = bind.execute(
            sa.text("SELECT 1 FROM categories WHERE user_id = :uid AND kind = 'refund'"),
            {"uid": user_id},
        ).fetchone()
        if not has_refund:
            bind.execute(
                sa.text(
                    "INSERT INTO categories (user_id, name, kind, is_seeded, color) "
                    "VALUES (:uid, 'Refund', 'refund', 1, '#0e9488')"
                ),
                {"uid": user_id},
            )


def downgrade() -> None:
    with op.batch_alter_table("categories") as batch_op:
        batch_op.drop_constraint("category_kind", type_="check")
        batch_op.create_check_constraint(
            "category_kind", "kind IN ('spend', 'income')"
        )
