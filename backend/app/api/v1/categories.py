"""Category routes (PRD §F5).

* ``GET /api/v1/categories`` — list active (non-archived) categories
  scoped to the current user, ordered by name ASC.
* ``POST /api/v1/categories`` — create user category. 409 on duplicate
  active name (the partial unique index ``uq_categories_active_user_name``
  carves out archived rows so soft-delete + recreate is fine).
* ``PATCH /api/v1/categories/{id}`` — rename. Seeded rows ARE renameable;
  ``is_seeded`` records origin, not identity. Empty body is a no-op
  (mirrors ``PATCH /transactions``). 409 on rename collision.
* ``DELETE /api/v1/categories/{id}`` — soft-delete (sets ``archived_at``).
  ``merchant_tag_map`` rows are **kept**: F3 auto-tag never resurrects an
  archived bucket because both ``prefetch_tag_map`` and ``GET /rules``
  filter ``Category.archived_at IS NULL`` (that filter is the load-bearing
  guard, not defence-in-depth). Keeping the rows means user-authored
  ``pinned`` rules survive an archive and would return if the category is
  ever un-archived. Idempotent: re-DELETE returns 404 because the archived
  row is filtered out of the loader query.

Cross-dialect 409 detection mirrors :mod:`app.api.v1.accounts`.

``kind`` (spend|income) is set on create and immutable (``CategoryUpdate``
has no ``kind`` field). Kind-matching between a transaction's
``category_id`` and its ``transaction_type`` IS enforced at the API for
every category-assignment path — POST/PATCH ``/transactions`` and
``POST /rules/categories`` all pass a ``kind`` to
:func:`app.services.category_service.validate_category_ids` (spend/
transfer → ``spend``, income → ``income``, via ``kind_for_type``). The
durable ``merchant_tag_map`` rule made UI-only enforcement insufficient (a
pinned income category would re-pollute every future spend import), so the
invariant is now DB-query enforced, not just UI-enforced.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUserId, SessionDep
from app.core import clock
from app.core.db_errors import is_unique_violation
from app.models import Category
from app.schemas import CategoryCreate, CategoryRead, CategoryUpdate

router = APIRouter(prefix="/categories", tags=["categories"])


def _is_name_dup(e: IntegrityError) -> bool:
    """Delegates the dialect-aware matching to
    :func:`app.core.db_errors.is_unique_violation`."""
    return is_unique_violation(
        e.orig,
        index_name="uq_categories_active_user_name",
        columns=["categories.user_id", "categories.name"],
    )


@router.get("", response_model=list[CategoryRead])
def list_categories(
    session: SessionDep,
    user_id: CurrentUserId,
) -> list[Category]:
    stmt = (
        select(Category)
        .where(Category.user_id == user_id, Category.archived_at.is_(None))
        .order_by(Category.name.asc())
    )
    return list(session.scalars(stmt))


@router.post("", response_model=CategoryRead, status_code=status.HTTP_201_CREATED)
def create_category(
    payload: CategoryCreate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> Category:
    category = Category(
        user_id=user_id,
        name=payload.name,
        kind=payload.kind,
        color=payload.color,
        is_seeded=False,
    )
    session.add(category)
    try:
        session.commit()
    except IntegrityError as e:
        session.rollback()
        if _is_name_dup(e):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="category name already exists",
            ) from e
        raise
    session.refresh(category)
    return category


@router.patch("/{category_id}", response_model=CategoryRead)
def update_category(
    category_id: int,
    payload: CategoryUpdate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> Category:
    category = session.scalar(
        select(Category).where(
            Category.id == category_id,
            Category.user_id == user_id,
            Category.archived_at.is_(None),
        )
    )
    if category is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="category not found",
        ) from None
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        # Empty body — no DB round-trip, no spurious updated_at bump.
        return category
    if "name" in updates and updates["name"] == category.name:
        # Rename to same name — same short-circuit reasoning.
        return category
    for field, value in updates.items():
        setattr(category, field, value)
    try:
        session.commit()
    except IntegrityError as e:
        session.rollback()
        if _is_name_dup(e):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="category name already exists",
            ) from e
        raise
    session.refresh(category)
    return category


@router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_category(
    category_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> None:
    category = session.scalar(
        select(Category).where(
            Category.id == category_id,
            Category.user_id == user_id,
            Category.archived_at.is_(None),
        )
    )
    if category is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="category not found",
        ) from None
    # Archive is a pure UPDATE (``archived_at``); ``merchant_tag_map`` rows are
    # KEPT. F3 auto-tag never resurrects an archived bucket because both
    # ``prefetch_tag_map`` and ``GET /rules`` filter ``Category.archived_at IS
    # NULL`` — that filter is the load-bearing guard. Keeping the rows preserves
    # user-authored ``pinned`` rules across an archive (they would return on a
    # future un-archive) instead of silently, permanently destroying them.
    # transactions.category_id is left pointing at the archived row — UI filters
    # via archived_at, history is preserved.
    category.archived_at = clock.utcnow()
    session.commit()
    return None
