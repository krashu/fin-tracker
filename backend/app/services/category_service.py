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

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.models import Category, CategoryKindStr, TransactionTypeStr

# The per-kind fallback bucket seeded by migrations 0003/0012 (PRD §F5). Resolved
# by NAME because that is already how :func:`default_category_id` finds it, and
# ``demo_seed._categories_by_kind`` hard-requires one active per kind — it raises
# without one. Named here so the three places that depend on the same fact (the
# import fallback, the demo-seed precondition, and the archive-cascade exemption
# in ``app.api.v1.categories``) cannot drift apart.
FALLBACK_CATEGORY_NAME = "Other"


def kind_for_type(transaction_type: TransactionTypeStr) -> CategoryKindStr:
    """The category ``kind`` a transaction of this type may carry.

    ``income`` draws income categories; ``spend`` / ``refund`` / ``transfer``
    draw spend (a refund nets against spend in the same category; a transfer
    normally carries no category but falls back to spend). Backend mirror of
    the frontend ``categoryKindForType`` (``frontend/lib/categories.ts``) — the
    single source of truth for which kind each of the four types assigns.
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


def default_category_id(
    session: Session, *, user_id: UUID, name: str, kind: CategoryKindStr = "spend"
) -> int | None:
    """The active category id named ``name``/``kind`` for ``user_id`` — the
    single source of truth for a commit-time fallback bucket (PRD §F5): the
    seeded spend "Other" for an untagged spend row (refunds included — they are
    spend rows carrying a positive amount), income "Cashback" for an untagged
    income row named cashback (``app.api.v1.imports.commit_import_batch``, both
    callers). ``None`` if the user archived or renamed the category away — the
    spend caller treats that as a hard failure (there's no further fallback);
    the income/Cashback caller doesn't, since an uncategorized income row is
    already legal."""
    return session.scalar(
        select(Category.id).where(
            Category.user_id == user_id,
            Category.name == name,
            Category.kind == kind,
            Category.archived_at.is_(None),
        )
    )
