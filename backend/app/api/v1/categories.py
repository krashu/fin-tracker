"""Category routes (PRD §F5).

Two-level hierarchy per :doc:`/docs/adr/0012-category-hierarchy`: ``parent_id``
is a nullable self-FK, depth capped at 2 and enforced here (the schema layer
carries no parent validation).

* ``GET /api/v1/categories`` — list active (non-archived) categories
  scoped to the current user, ordered by name ASC. ``?kind=`` filters to
  spend or income. ``?tree=true`` nests subcategories under their parent
  (:class:`app.schemas.CategoryTreeRead`); a child whose parent is absent
  from the result set (archived, or filtered out by ``kind``) is promoted
  to root rather than dropped.
* ``POST /api/v1/categories`` — create user category, optionally under
  ``parent_id``. 409 on duplicate active name (the partial unique index
  ``uq_categories_active_user_name`` carves out archived rows so
  soft-delete + recreate is fine). A parent must exist, be a root itself
  (depth cap), and share ``kind`` — else 422.
* ``PATCH /api/v1/categories/{id}`` — rename, recolor, and/or reparent.
  Seeded rows ARE renameable; ``is_seeded`` records origin, not identity.
  ``parent_id`` may be set (with the same root/depth/kind checks as
  create), or set to ``null`` to promote a subcategory to root — that
  promotion is a plain PATCH, never a delete (see
  :attr:`app.models.Category.subcategories`). A category with active
  children cannot itself be given a parent. Empty body is a no-op (mirrors
  ``PATCH /transactions``). 409 on rename collision.
* ``DELETE /api/v1/categories/{id}`` — soft-delete (sets ``archived_at``).
  Archiving a parent cascades to its active children in the same request
  (one hop, forward only — archiving a child never touches the parent).
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
from app.services.category_service import FALLBACK_CATEGORY_NAME

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

    # Tree view: nest subcategories under parents. A child whose parent is
    # missing from THIS result set (the parent is archived, or was filtered
    # out by `kind` — both parent and child always share kind, so in
    # practice this is "parent archived", e.g. via backup restore) is
    # promoted to root rather than dropped — ADR-0012 forbids a tree
    # endpoint from silently losing a row. The frontend tree builder
    # (`lib/categories.ts` `buildCategoryTree`) does the same; the two must
    # agree.
    all_ids = {c.id for c in categories}
    parents: list[CategoryTreeRead] = []
    children_by_parent: dict[int, list[CategoryRead]] = {}
    for c in categories:
        if c.parent_id is not None and c.parent_id in all_ids:
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
                    parent_id=c.parent_id,
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
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="parent category not found",
            )
        if parent.parent_id is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="cannot nest category more than 2 levels deep",
            )
        if parent.kind != payload.kind:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
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

    # Gate on key PRESENCE alone, not truthiness — `parent_id: null` (promote
    # to root) is a legitimate, validation-free write and must not fall
    # through this block by accident. Only the *lookup* branch below is
    # skipped for null; the asymmetry used to be the bug (ADR-0012).
    if "parent_id" in updates:
        target_parent_id = updates["parent_id"]
        if target_parent_id is not None:
            if target_parent_id == category_id:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="category cannot be its own parent",
                )
            # Check if category already has active children — giving THIS
            # category a parent would nest its children 3 deep.
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
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
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
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="parent category not found",
                )
            if parent.parent_id is not None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="cannot nest category more than 2 levels deep",
                )
            if parent.kind != category.kind:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
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
    # naive_utcnow, not utcnow — this is written and later read back /
    # compared (see `archived_at IS NULL` filters app-wide); an aware value
    # would assignment-cast through the server's TimeZone on Postgres v2 and
    # come back shifted (ADR-0001 rule 5).
    now = clock.naive_utcnow()
    category.archived_at = now

    # Also archive any active subcategories if a parent is archived — EXCEPT the
    # kind's fallback bucket. `demo_seed._categories_by_kind` requires an active
    # "Other" per kind and RAISES without one, and archiving is one-way: nothing
    # in the API or UI clears `archived_at` (`CategoryUpdate` has no such field
    # and PATCH 404s on an archived row). So a user archiving `Income` — whose
    # child the income "Other" is — took the fallback with it and broke demo
    # seeding on every later startup, permanently, without ever naming it.
    # Exempting it leaves an active child under an archived parent, which is
    # already a handled state: `list_categories` promotes such a child to root
    # (see the tree-view comment above), as does the frontend `buildCategoryTree`.
    # A direct DELETE of the fallback itself is still honoured — that is an
    # explicit act on that category, not a silent side effect of archiving another.
    subcategories = session.scalars(
        select(Category).where(
            Category.parent_id == category_id,
            Category.user_id == user_id,
            Category.archived_at.is_(None),
            Category.name != FALLBACK_CATEGORY_NAME,
        )
    ).all()
    for sub in subcategories:
        sub.archived_at = now

    session.commit()
    return None
