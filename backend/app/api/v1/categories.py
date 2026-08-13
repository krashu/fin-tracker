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
from app.models import Category, CategoryKindStr
from app.schemas import CategoryCreate, CategoryRead, CategoryTreeRead, CategoryUpdate

router = APIRouter(prefix="/categories", tags=["categories"])


def _is_name_dup(e: IntegrityError) -> bool:
    """Delegates the dialect-aware matching to
    :func:`app.core.db_errors.is_unique_violation`."""
    return is_unique_violation(
        e.orig,
        index_name="uq_categories_active_user_name",
        columns=["categories.user_id", "categories.name"],
    )


@router.get("", response_model=None)
def list_categories(
    session: SessionDep,
    user_id: CurrentUserId,
    kind: CategoryKindStr | None = None,
    tree: bool = False,
) -> list[CategoryTreeRead] | list[CategoryRead]:
    stmt = select(Category).where(Category.user_id == user_id, Category.archived_at.is_(None))
    if kind is not None:
        stmt = stmt.where(Category.kind == kind)
    stmt = stmt.order_by(Category.name.asc())
    categories = list(session.scalars(stmt))

    if not tree:
        return [CategoryRead.model_validate(c) for c in categories]

    # Tree view: nest subcategories under parents
    parents: list[CategoryTreeRead] = []
    children_by_parent: dict[int, list[CategoryRead]] = {}
    for c in categories:
        if c.parent_id is not None:
            children_by_parent.setdefault(c.parent_id, []).append(CategoryRead.model_validate(c))
        else:
            parents.append(
                CategoryTreeRead(
                    id=c.id,
                    name=c.name,
                    kind=c.kind,
                    is_seeded=c.is_seeded,
                    archived_at=c.archived_at,
                    color=c.color,
                    parent_id=None,
                    subcategories=[],
                )
            )

    for p in parents:
        p.subcategories = children_by_parent.get(p.id, [])

    return parents


@router.post("", response_model=CategoryRead, status_code=status.HTTP_201_CREATED)
def create_category(
    payload: CategoryCreate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> Category:
    if payload.parent_id is not None:
        parent = session.scalar(
            select(Category).where(
                Category.id == payload.parent_id,
                Category.user_id == user_id,
                Category.archived_at.is_(None),
            )
        )
        if parent is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="parent category not found",
            )
        if parent.parent_id is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="cannot nest category more than 2 levels deep",
            )
        if parent.kind != payload.kind:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="category kind must match parent category kind",
            )

    category = Category(
        user_id=user_id,
        name=payload.name,
        kind=payload.kind,
        color=payload.color,
        parent_id=payload.parent_id,
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

    if "parent_id" in updates and updates["parent_id"] is not None:
        target_parent_id = updates["parent_id"]
        if target_parent_id == category_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="category cannot be its own parent",
            )
        # Check if category already has active children
        has_children = session.scalar(
            select(Category.id)
            .where(
                Category.parent_id == category_id,
                Category.user_id == user_id,
                Category.archived_at.is_(None),
            )
            .limit(1)
        )
        if has_children is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="category has subcategories and cannot be assigned a parent",
            )
        parent = session.scalar(
            select(Category).where(
                Category.id == target_parent_id,
                Category.user_id == user_id,
                Category.archived_at.is_(None),
            )
        )
        if parent is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="parent category not found",
            )
        if parent.parent_id is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="cannot nest category more than 2 levels deep",
            )
        if parent.kind != category.kind:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="category kind must match parent category kind",
            )

    if "name" in updates and updates["name"] == category.name:
        del updates["name"]

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
    now = clock.utcnow()
    category.archived_at = now

    # Also archive any active subcategories if a parent is archived
    subcategories = session.scalars(
        select(Category).where(
            Category.parent_id == category_id,
            Category.user_id == user_id,
            Category.archived_at.is_(None),
        )
    ).all()
    for sub in subcategories:
        sub.archived_at = now

    session.commit()
    return None
