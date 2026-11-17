"""fallback "Other" roots per kind; cashback moves from income to spend

Revision ID: 0039_fallback_roots_and_cashback_spend
Revises: 0038_pairs_refund_link_fx_stamp
Create Date: 2026-11-15

Data only, no DDL. For every user, in this order:

**1. Fallback roots (ADR-0012 rule 8).** Each kind's "Other" is an unarchivable root. 0034
reparented the income "Other" under "Income"; this step makes the active "Other" of each
kind a root again, giving it the frozen seed hex ``#94a3b8`` only if its colour is NULL (a
colour the user picked is kept). A user with no active "Other" of a kind — archived, or
renamed away — gets a fresh seeded root "Other"; the archived or renamed row is left alone.
The partial unique index on active names means there is at most one active "Other" per
kind to find.

**2. Cashback is a positive spend (ADR-0013 rule 5).** The target is the user's active
spend "Cashback" if one exists (reused as is, never recoloured or reparented), else a new
seeded root spend "Cashback" ``#c98500``. Every ``income`` row in any income category named
"Cashback", active or archived, is retyped ``spend`` and moved to the target, keeping its
sign: a pure retype. The fingerprint never hashed ``transaction_type`` or ``category_id``
(ADR-0006 rule 4), so no fingerprint, ``origin_fingerprint`` or ``occurrence`` moves.
``auto_category_id`` and ``merchant_tag_map`` are spend-only, so neither can point at an
income category. Finally the active income "Cashback" and its active children are archived
(ADR-0012: archive, never delete).

Constants are frozen copies, never imported from ``app`` (migrations are snapshots).
Timestamps are naive UTC (ADR-0001 rule 5); every UPDATE restates ``user_id``.

**Downgrade.** Step 1 is a documented no-op: a root "Other" is a valid pre-revision state,
and which row used to be a child (or used to be the only "Other") is not recorded anywhere.
Step 2 is reconstructive (like 0029's), per user: every ``spend`` row in the active spend
"Cashback" goes back to ``income`` in the active income "Cashback" — or, failing that, the
most recently archived one, un-archived — keeping its sign. A user with no income "Cashback"
at all keeps their rows as they are. The spend "Cashback" is then deleted if it is seeded
and nothing references it (transactions, tag rules, child categories), else archived. Rows
a user put in spend "Cashback" after the upgrade come back as income too: deterministic, and
the sign every aggregate reads is untouched.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0039_fallback_roots_and_cashback_spend"
down_revision: str | Sequence[str] | None = "0038_pairs_refund_link_fx_stamp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FALLBACK_NAME = "Other"
_FALLBACK_COLOR = "#94a3b8"
_CASHBACK_NAME = "Cashback"
_CASHBACK_COLOR = "#c98500"

_users = sa.table("users", sa.column("id", sa.Uuid()))

_transactions = sa.table(
    "transactions",
    sa.column("id", sa.Integer()),
    sa.column("user_id", sa.Uuid()),
    sa.column("category_id", sa.Integer()),
    sa.column("auto_category_id", sa.Integer()),
    sa.column("transaction_type", sa.String()),
)

_merchant_tag_map = sa.table(
    "merchant_tag_map",
    sa.column("id", sa.Integer()),
    sa.column("user_id", sa.Uuid()),
    sa.column("category_id", sa.Integer()),
)

_categories = sa.table(
    "categories",
    sa.column("id", sa.Integer()),
    sa.column("user_id", sa.Uuid()),
    sa.column("name", sa.String()),
    sa.column("kind", sa.String()),
    sa.column("is_seeded", sa.Boolean()),
    sa.column("color", sa.String()),
    sa.column("parent_id", sa.Integer()),
    sa.column("archived_at", sa.DateTime()),
)


def _root_fallbacks(bind: sa.Connection, user_id: object) -> None:
    """Leave ``user_id`` with exactly one active root "Other" per kind."""
    for kind in ("spend", "income"):
        row = bind.execute(
            sa.select(_categories.c.id, _categories.c.parent_id, _categories.c.color).where(
                _categories.c.user_id == user_id,
                _categories.c.name == _FALLBACK_NAME,
                _categories.c.kind == kind,
                _categories.c.archived_at.is_(None),
            )
        ).first()
        if row is None:
            bind.execute(
                _categories.insert().values(
                    user_id=user_id,
                    name=_FALLBACK_NAME,
                    kind=kind,
                    is_seeded=True,
                    color=_FALLBACK_COLOR,
                    parent_id=None,
                )
            )
        elif row.parent_id is not None:
            values: dict[str, object] = {"parent_id": None}
            if row.color is None:
                values["color"] = _FALLBACK_COLOR
            bind.execute(
                _categories.update()
                .where(_categories.c.id == row.id, _categories.c.user_id == user_id)
                .values(**values)
            )


def _active_category_id(bind: sa.Connection, user_id: object, name: str, kind: str) -> int | None:
    return bind.execute(
        sa.select(_categories.c.id).where(
            _categories.c.user_id == user_id,
            _categories.c.name == name,
            _categories.c.kind == kind,
            _categories.c.archived_at.is_(None),
        )
    ).scalar()


def _cashback_to_spend(bind: sa.Connection, user_id: object, now: datetime) -> None:
    """Retype the user's income-Cashback rows to positive spend in spend "Cashback"."""
    target = _active_category_id(bind, user_id, _CASHBACK_NAME, "spend")
    if target is None:
        target = bind.execute(
            _categories.insert()
            .values(
                user_id=user_id,
                name=_CASHBACK_NAME,
                kind="spend",
                is_seeded=True,
                color=_CASHBACK_COLOR,
                parent_id=None,
            )
            .returning(_categories.c.id)
        ).scalar_one()

    sources = bind.execute(
        sa.select(_categories.c.id, _categories.c.archived_at).where(
            _categories.c.user_id == user_id,
            _categories.c.name == _CASHBACK_NAME,
            _categories.c.kind == "income",
        )
    ).all()
    if not sources:
        return
    bind.execute(
        _transactions.update()
        .where(
            _transactions.c.user_id == user_id,
            _transactions.c.category_id.in_([s.id for s in sources]),
            _transactions.c.transaction_type == "income",
        )
        .values(transaction_type="spend", category_id=target)
    )
    active_sources = [s.id for s in sources if s.archived_at is None]
    if active_sources:
        bind.execute(
            _categories.update()
            .where(
                _categories.c.user_id == user_id,
                _categories.c.archived_at.is_(None),
                sa.or_(
                    _categories.c.id.in_(active_sources),
                    _categories.c.parent_id.in_(active_sources),
                ),
            )
            .values(archived_at=now)
        )


def _is_referenced(bind: sa.Connection, user_id: object, category_id: int) -> bool:
    """True if a transaction, a tag rule or a child category still points at this one."""
    probes = (
        sa.select(sa.literal(1)).where(
            _transactions.c.user_id == user_id,
            sa.or_(
                _transactions.c.category_id == category_id,
                _transactions.c.auto_category_id == category_id,
            ),
        ),
        sa.select(sa.literal(1)).where(
            _merchant_tag_map.c.user_id == user_id,
            _merchant_tag_map.c.category_id == category_id,
        ),
        sa.select(sa.literal(1)).where(
            _categories.c.user_id == user_id, _categories.c.parent_id == category_id
        ),
    )
    return any(bind.execute(probe.limit(1)).first() is not None for probe in probes)


def _cashback_back_to_income(bind: sa.Connection, user_id: object, now: datetime) -> None:
    """Reverse :func:`_cashback_to_spend` for one user (see the module docstring)."""
    source = _active_category_id(bind, user_id, _CASHBACK_NAME, "spend")
    if source is None:
        return
    target = _active_category_id(bind, user_id, _CASHBACK_NAME, "income")
    if target is None:
        target = bind.execute(
            sa.select(_categories.c.id)
            .where(
                _categories.c.user_id == user_id,
                _categories.c.name == _CASHBACK_NAME,
                _categories.c.kind == "income",
            )
            .order_by(_categories.c.archived_at.desc(), _categories.c.id.desc())
            .limit(1)
        ).scalar()
        if target is None:
            return  # no income Cashback ever existed: leave the rows where they are
        bind.execute(
            _categories.update()
            .where(_categories.c.id == target, _categories.c.user_id == user_id)
            .values(archived_at=None)
        )

    bind.execute(
        _transactions.update()
        .where(
            _transactions.c.user_id == user_id,
            _transactions.c.category_id == source,
            _transactions.c.transaction_type == "spend",
        )
        .values(transaction_type="income", category_id=target)
    )
    is_seeded = bind.execute(
        sa.select(_categories.c.is_seeded).where(_categories.c.id == source)
    ).scalar_one()
    if is_seeded and not _is_referenced(bind, user_id, source):
        bind.execute(
            _categories.delete().where(_categories.c.id == source, _categories.c.user_id == user_id)
        )
    else:
        bind.execute(
            _categories.update()
            .where(_categories.c.id == source, _categories.c.user_id == user_id)
            .values(archived_at=now)
        )


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(UTC).replace(tzinfo=None)  # naive UTC — ADR-0001 rule 5
    for user_id in bind.execute(sa.select(_users.c.id)).scalars().all():
        _root_fallbacks(bind, user_id)
        _cashback_to_spend(bind, user_id, now)


def downgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(UTC).replace(tzinfo=None)  # naive UTC — ADR-0001 rule 5
    for user_id in bind.execute(sa.select(_users.c.id)).scalars().all():
        # Step 1 (fallback roots) is intentionally not reversed — see the module docstring.
        _cashback_back_to_income(bind, user_id, now)
