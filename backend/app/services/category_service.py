"""Category validity checks shared across routers.

The single source of truth for "what makes a category id usable" — it exists, is
owned by the user, and is not archived. Extracted from
:mod:`app.api.v1.transactions` so the identical rule serves both transaction
category assignment (POST / PATCH ``/transactions``) and rule authoring
(``POST /rules/categories``) without a divergent second copy.

Returns data (the valid subset); the HTTP 422 mapping stays in each router — this
service layer never raises ``HTTPException`` (the project keeps HTTP concerns out
of services).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.models import Category, CategoryKindStr, TransactionTypeStr

# The per-kind fallback bucket (PRD §F5): an unarchivable, unrenameable root, one
# active per kind (ADR-0012 rule 8). Resolved by NAME + kind. Named here so the
# places that depend on the same fact (the import fallback, the demo-seed
# precondition, and the archive / parent / rename guards in
# ``app.api.v1.categories``) cannot drift apart.
FALLBACK_CATEGORY_NAME = "Other"

# The seeded spend-kind root a cashback credit is pre-filled into at import, and
# the category the CC YTD block counts as cashback (ADR-0013). Resolved by NAME +
# kind "spend"; unlike the fallback it may be archived or renamed, and then a
# cashback credit is staged like any other positive spend.
CASHBACK_CATEGORY_NAME = "Cashback"

# Spend Seam (Milestone 1 Step 3): system category for bank debits funding investments.
# Excluded from living expense aggregates in dashboards and overview.
INVESTMENT_TRANSFER_CATEGORY_NAME = "system:investment_transfer"

# What a category *is* to the app, computed here and sent on the wire as
# ``CategoryRead.role`` so the frontend never matches names (ADR-0012 rules 6 and 8,
# ADR-0013).
CategoryRoleStr = Literal["fallback", "cashback", "investment_transfer"]


def is_fallback_category(category: Category) -> bool:
    """True for the kind's fallback "Other" (ADR-0012 rule 8).

    The one predicate the archive, rename and parent guards in
    :mod:`app.api.v1.categories` and :func:`category_role` share. On an active row
    the name alone identifies it: the active unique index
    ``uq_categories_active_user_name (user_id, name, kind)`` leaves no room for a
    second active "Other" of the same kind, and rule 8 forbids it a parent.
    """
    return category.name == FALLBACK_CATEGORY_NAME


def is_system_category(category: Category) -> bool:
    """True for protected system categories like system:investment_transfer."""
    return category.name == INVESTMENT_TRANSFER_CATEGORY_NAME or category.name.startswith("system:")


def get_or_create_investment_transfer_category(session: Session, *, user_id: UUID) -> Category:
    """Return the system:investment_transfer category for user_id, creating it if absent."""
    cat = session.scalar(
        select(Category).where(
            Category.user_id == user_id,
            Category.name == INVESTMENT_TRANSFER_CATEGORY_NAME,
            Category.kind == "spend",
        )
    )
    if cat is not None:
        return cat
    cat = Category(
        user_id=user_id,
        name=INVESTMENT_TRANSFER_CATEGORY_NAME,
        kind="spend",
        is_seeded=True,
        color="#64748b",
        parent_id=None,
    )
    session.add(cat)
    session.flush()
    return cat


def get_or_create_fallback_category(
    session: Session, *, user_id: UUID, kind: CategoryKindStr = "spend"
) -> Category:
    """Return the fallback "Other" category for user_id and kind, creating it if absent."""
    cat = session.scalar(
        select(Category).where(
            Category.user_id == user_id,
            Category.name == FALLBACK_CATEGORY_NAME,
            Category.kind == kind,
            Category.archived_at.is_(None),
        )
    )
    if cat is not None:
        return cat
    cat = Category(
        user_id=user_id,
        name=FALLBACK_CATEGORY_NAME,
        kind=kind,
        is_seeded=True,
        color="#94a3b8",
        parent_id=None,
    )
    session.add(cat)
    session.flush()
    return cat


def category_role(
    *, name: str, kind: CategoryKindStr, parent_name: str | None
) -> CategoryRoleStr | None:
    """The role of one category row; the pure rule behind :func:`category_roles`.

    ``fallback`` is checked first (the names differ, so they never overlap).
    ``investment_transfer`` is checked next for system investment transfer category.
    ``cashback`` is the spend-kind "Cashback" or a child of it, whatever the
    parent's archive state: the Python twin of the SQL ``is_cashback`` predicate in
    the credit-card YTD block of :mod:`app.api.v1.dashboards` (ADR-0013 rule 4).
    A child's kind equals its parent's (ADR-0012 rule 3), so the parent's kind
    needs no separate check.
    """
    if name == FALLBACK_CATEGORY_NAME:
        return "fallback"
    if name == INVESTMENT_TRANSFER_CATEGORY_NAME:
        return "investment_transfer"
    if kind == "spend" and CASHBACK_CATEGORY_NAME in (name, parent_name):
        return "cashback"
    return None


def category_roles(
    session: Session, *, categories: Sequence[Category], user_id: UUID
) -> dict[int, CategoryRoleStr | None]:
    """The role of each of ``categories``, keyed by category id.

    One query resolves the parents' names. It has no ``archived_at`` filter on
    purpose: a child of an archived Cashback is still cashback (see
    :func:`category_role`). ``user_id`` is restated per ADR-0003 even though the
    ids come from the user's own rows.
    """
    parent_ids = {c.parent_id for c in categories if c.parent_id is not None}
    parent_names: dict[int, str] = {}
    if parent_ids:
        parent_names = {
            pid: pname
            for pid, pname in session.execute(
                select(Category.id, Category.name).where(
                    Category.id.in_(parent_ids), Category.user_id == user_id
                )
            )
        }
    return {
        c.id: category_role(
            name=c.name,
            kind=c.kind,
            parent_name=parent_names.get(c.parent_id) if c.parent_id is not None else None,
        )
        for c in categories
    }


def kind_for_type(transaction_type: TransactionTypeStr) -> CategoryKindStr:
    """The category ``kind`` a transaction of this type may carry.

    ``income`` draws income categories; ``spend`` and ``transfer`` draw spend
    (a spend row of either sign — a refund or a cashback is a positive spend,
    ADR-0009 / ADR-0013 — nets in its category; a transfer normally carries no
    category but falls back to spend). Backend mirror of the frontend
    ``categoryKindForType`` (``frontend/lib/categories.ts``) — the single source
    of truth for which kind each of the three types assigns.
    """
    return "income" if transaction_type == "income" else "spend"


def validate_category_ids(
    session: Session,
    *,
    category_ids: Iterable[int],
    user_id: UUID,
    kind: CategoryKindStr | None = None,
) -> set[int]:
    """Return the subset of ``category_ids`` that exist, are owned by ``user_id``,
    and are not archived. When ``kind`` is given, also require ``Category.kind ==
    kind`` (spend/income) so a wrong-scope category is rejected — leaving it
    ``None`` keeps the pre-existing kind-blind behaviour. Empty input → empty set
    (no query issued)."""
    ids = set(category_ids)
    if not ids:
        return set()
    stmt = select(Category.id).where(
        Category.id.in_(ids),
        Category.user_id == user_id,
        Category.archived_at.is_(None),
    )
    if kind is not None:
        stmt = stmt.where(Category.kind == kind)
    return set(session.scalars(stmt))


def resolve_category_labels(
    session: Session, *, category_ids: Iterable[int], user_id: UUID
) -> dict[int, tuple[str, str | None]]:
    """Map each owned ``category_id`` to ``(name, parent_name)`` for **display**.

    Deliberately does **not** filter ``archived_at`` — the opposite of
    :func:`validate_category_ids`, and the difference is the point. Archiving is
    soft (:doc:`/docs/adr/0012-category-hierarchy`): a transaction keeps its
    ``category_id`` when its category is archived, and ``DELETE /categories``
    promises exactly that ("existing transactions will keep their historical
    categories"). But ``GET /categories`` returns active rows only, so the
    frontend cannot name an archived category and rendered it as
    "Uncategorized" — telling the user an assignment was lost that was not.

    Same shape as the join behind ``GET /dashboards/spend-by-category``, which
    already surfaces an archived category's stored name and is pinned by
    ``test_archived_category_surfaces_with_stored_name``. Validation still runs
    through ``validate_category_ids``; naming a row is not permission to assign
    it, so the two must stay separate.

    ``user_id`` is still restated (ADR-0003 rule 1) — an unowned id is simply
    absent from the result, never named. One batched query; empty input → empty
    dict with no query issued.
    """
    ids = set(category_ids)
    if not ids:
        return {}
    parent = aliased(Category)
    rows = session.execute(
        select(Category.id, Category.name, parent.name)
        .outerjoin(parent, Category.parent_id == parent.id)
        .where(Category.id.in_(ids), Category.user_id == user_id)
    ).all()
    return {cid: (name, parent_name) for cid, name, parent_name in rows}


def fallback_category_id(session: Session, *, user_id: UUID, kind: CategoryKindStr) -> int:
    """The id of ``user_id``'s active fallback "Other" of ``kind`` (PRD §F5).

    It always exists — it cannot be archived, renamed or reparented (ADR-0012
    rule 8) — so a miss is a broken invariant and ``.one()`` raises (a 500), not
    a 422 for the caller to map.
    """
    return session.scalars(
        select(Category.id).where(
            Category.user_id == user_id,
            Category.name == FALLBACK_CATEGORY_NAME,
            Category.kind == kind,
            Category.archived_at.is_(None),
        )
    ).one()


def default_category_id(
    session: Session, *, user_id: UUID, name: str, kind: CategoryKindStr = "spend"
) -> int | None:
    """The active category id named ``name``/``kind`` for ``user_id``, or
    ``None`` if the user archived or renamed it away.

    Used for the spend "Cashback" a cashback credit is pre-filled into
    (ADR-0013): at staging (``import_service.import_statement``) and at commit
    (``app.api.v1.imports.commit_import_batch``, to recognise that prefill).
    ``None`` there means the credit is staged like any other positive spend.
    The fallback "Other" always exists and has its own lookup,
    :func:`fallback_category_id`."""
    return session.scalar(
        select(Category.id).where(
            Category.user_id == user_id,
            Category.name == name,
            Category.kind == kind,
            Category.archived_at.is_(None),
        )
    )
