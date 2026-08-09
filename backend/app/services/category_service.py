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
from sqlalchemy.orm import Session

from app.models import Category, CategoryKindStr, TransactionTypeStr


def kind_for_type(transaction_type: TransactionTypeStr) -> CategoryKindStr:
    """The category ``kind`` a transaction of this type may carry.

    ``income`` draws income categories; ``refund`` draws refund categories;
    ``spend`` / ``transfer`` draw spend categories. Backend mirror of
    the frontend ``categoryKindForType`` (``frontend/lib/categories.ts``) — the
    single source of truth for which kind each of the four types assigns.
    """
    if transaction_type == "income":
        return "income"
    if transaction_type == "refund":
        return "refund"
    return "spend"


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
