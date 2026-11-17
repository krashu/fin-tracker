"""retire the seeded spend category "Credit Card Payments"

Revision ID: 0047_retire_card_bill_category
Revises: 0046_root_category_color
Create Date: 2026-11-17

Data only, no DDL (ADR-0002 rule 7b, ADR-0012). The default taxonomy no longer carries a
card-bill spend category: a bill payment is an unpaired ``transfer`` out of spend, never spend
in a category, and a tag rule learned from a categorised bill would prefill the next one.
Provisioning stops seeding it; this archives the row every existing user already has.

**Upgrade**, per user: set ``archived_at = now`` on the active seeded spend "Credit Card
Payments". If a user promoted it to a root, its active children are archived with the same
stamp (ADR-0012 rule 9: archiving a root archives its subtree). A user-made category of the
same name (``is_seeded`` false) is left alone. No row moves: a transaction in it keeps the
category and its type (it stays in spend-by-category by ADR-0004 rule 11 and rolls up under its
parent by ADR-0012 rule 6), and ``merchant_tag_map`` rows pointing at it stay (prefill skips an
archived category, ADR-0004 rule 8). Pickers hide it.

**Downgrade**, per user with no active spend "Credit Card Payments": un-archive the most
recently archived seeded one. Children stay archived. Two accepted gaps, the same shape as
0039: a seed the user archived themselves before 0047 comes back too, because ``archived_at``
does not record who archived it, and a promoted root's children are not restored.

Constants are frozen copies, never imported from ``app`` (migrations are snapshots). Timestamps
are naive UTC (ADR-0001 rule 5); every UPDATE restates ``user_id``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0047_retire_card_bill_category"
down_revision: str | Sequence[str] | None = "0046_root_category_color"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "Credit Card Payments"

_users = sa.table("users", sa.column("id", sa.Uuid()))

_categories = sa.table(
    "categories",
    sa.column("id", sa.Integer()),
    sa.column("user_id", sa.Uuid()),
    sa.column("name", sa.String()),
    sa.column("kind", sa.String()),
    sa.column("is_seeded", sa.Boolean()),
    sa.column("parent_id", sa.Integer()),
    sa.column("archived_at", sa.DateTime()),
)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _card_bill_rows(user_id: object, *, active: bool):  # noqa: ANN202
    """The user's spend "Credit Card Payments" rows (seeded or not), active or archived."""
    return sa.select(
        _categories.c.id,
        _categories.c.parent_id,
        _categories.c.is_seeded,
        _categories.c.archived_at,
    ).where(
        _categories.c.user_id == user_id,
        _categories.c.name == _NAME,
        _categories.c.kind == "spend",
        _categories.c.archived_at.is_(None) if active else _categories.c.archived_at.is_not(None),
    )


def upgrade() -> None:
    bind = op.get_bind()
    now = _now()
    for user_id in bind.execute(sa.select(_users.c.id)).scalars().all():
        for row in bind.execute(_card_bill_rows(user_id, active=True)).all():
            if not row.is_seeded:
                continue
            bind.execute(
                _categories.update()
                .where(_categories.c.id == row.id, _categories.c.user_id == user_id)
                .values(archived_at=now)
            )
            if row.parent_id is None:
                bind.execute(
                    _categories.update()
                    .where(
                        _categories.c.parent_id == row.id,
                        _categories.c.user_id == user_id,
                        _categories.c.archived_at.is_(None),
                    )
                    .values(archived_at=now)
                )


def downgrade() -> None:
    bind = op.get_bind()
    for user_id in bind.execute(sa.select(_users.c.id)).scalars().all():
        if bind.execute(_card_bill_rows(user_id, active=True)).first() is not None:
            continue
        latest = bind.execute(
            _card_bill_rows(user_id, active=False)
            .where(_categories.c.is_seeded.is_(True))
            .order_by(_categories.c.archived_at.desc(), _categories.c.id.desc())
        ).first()
        if latest is None:
            continue
        bind.execute(
            _categories.update()
            .where(_categories.c.id == latest.id, _categories.c.user_id == user_id)
            .values(archived_at=None)
        )
