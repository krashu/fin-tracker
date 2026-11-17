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
* ``GET /api/v1/categories/palette`` — the palette colours in slot order
  (:class:`app.schemas.CategoryPaletteRead`); the frontend offers exactly these.
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
  ``PATCH /transactions``). 409 on rename collision. Each kind's fallback
  "Other" can be recoloured but never renamed or given a parent — 422
  (ADR-0012 rule 8). A root always has a color: omit it on create and the server
  assigns the least-used palette slot of its kind; null on a root (create, PATCH,
  promotion) is a 422; a promotion of a colourless subcategory assigns one
  (ADR-0012 rule 7).
* ``DELETE /api/v1/categories/{id}`` — soft-delete (sets ``archived_at``).
  422 on a kind's fallback "Other", which always stays active (ADR-0012
  rule 8). Archiving a parent cascades to **all** its active children in the
  same request with one shared stamp (one hop, forward only — archiving a
  child never touches the parent). ``?reassign_to=<id>`` also moves every
  **board** row on the archived categories (parent and cascaded children) to a
  live, same-kind target outside that set, in the same commit, teaching once
  per canonical merchant (ADR-0012 rule 10 / ADR-0004 rule 4); an invalid
  target is a 422 before anything is written. Pending (staged) rows are left
  alone — the import commit defaults them to "Other". Archive is one-way: no endpoint clears
  ``archived_at``. ``merchant_tag_map`` rows are **kept**: F3 auto-tag never
  resurrects an archived bucket because both ``prefetch_tag_map`` and
  ``GET /rules`` filter ``Category.archived_at IS NULL`` (that filter is the
  load-bearing guard, not defence-in-depth). Idempotent: re-DELETE returns
  404 because the archived row is filtered out of the loader query.

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

from collections.abc import Sequence
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import CurrentUserId, SessionDep
from app.core import clock
from app.core.db_errors import is_unique_violation
from app.models import Category, CategoryKindStr, Transaction
from app.schemas import (
    CategoryCreate,
    CategoryPaletteRead,
    CategoryRead,
    CategoryTreeRead,
    CategoryUpdate,
)
from app.services.bulk_categorise import bulk_categorise
from app.services.category_colors import (
    CATEGORY_PALETTE,
    active_root_colors,
    category_colors,
    next_root_color,
)
from app.services.category_service import (
    category_roles,
    is_fallback_category,
    validate_category_ids,
)
from app.services.merchant_alias import load_alias_resolver
from app.services.transaction_queries import confirmed_only

router = APIRouter(prefix="/categories", tags=["categories"])


def _is_name_dup(e: IntegrityError) -> bool:
    """Delegates the dialect-aware matching to
    :func:`app.core.db_errors.is_unique_violation`."""
    return is_unique_violation(
        e.orig,
        index_name="uq_categories_active_user_name",
        columns=["categories.user_id", "categories.name"],
    )


def _reads(session: Session, categories: Sequence[Category], user_id: UUID) -> list[CategoryRead]:
    """Build the read schema for ``categories`` with each one's ``role`` and ``display_color``.

    Both are required on ``CategoryRead``, so this is the only way a response gets built.
    """
    roles = category_roles(session, categories=categories, user_id=user_id)
    colors = category_colors(session, user_id=user_id)
    return [
        CategoryRead(
            id=c.id,
            name=c.name,
            kind=c.kind,
            is_seeded=c.is_seeded,
            archived_at=c.archived_at,
            color=c.color,
            display_color=colors[c.id].display,
            parent_id=c.parent_id,
            role=roles[c.id],
        )
        for c in categories
    ]


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

    reads = {r.id: r for r in _reads(session, categories, user_id)}
    if not tree:
        return [reads[c.id] for c in categories]

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
            children_by_parent.setdefault(c.parent_id, []).append(reads[c.id])
        else:
            parents.append(CategoryTreeRead(**reads[c.id].model_dump(), subcategories=[]))

    for p in parents:
        p.subcategories = children_by_parent.get(p.id, [])

    return parents


@router.get("/palette", response_model=CategoryPaletteRead)
def get_category_palette(_user_id: CurrentUserId) -> CategoryPaletteRead:
    return CategoryPaletteRead(colors=list(CATEGORY_PALETTE))


@router.post("", response_model=CategoryRead, status_code=status.HTTP_201_CREATED)
def create_category(
    payload: CategoryCreate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> CategoryRead:
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

    color = payload.color
    if payload.parent_id is None:
        if "color" in payload.model_fields_set and payload.color is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="a root category must have a color",
            )
        color = payload.color or next_root_color(
            active_root_colors(session, user_id=user_id, kind=payload.kind)
        )

    category = Category(
        user_id=user_id,
        name=payload.name,
        kind=payload.kind,
        color=color,
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
    return _reads(session, [category], user_id)[0]


@router.patch("/{category_id}", response_model=CategoryRead)
def update_category(
    category_id: int,
    payload: CategoryUpdate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> CategoryRead:
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
        return _reads(session, [category], user_id)[0]

    # The fallback "Other" is found by name + kind and must stay a root (ADR-0012
    # rule 8), so it keeps both. Checked before the parent lookup: the answer does
    # not depend on which parent was asked for.
    if is_fallback_category(category):
        if updates.get("parent_id") is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="the fallback category cannot have a parent",
            )
        if "name" in updates and updates["name"] != category.name:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="the fallback category cannot be renamed",
            )

    ends_as_root = (
        updates["parent_id"] is None if "parent_id" in updates else category.parent_id is None
    )
    promotes = (
        "parent_id" in updates and updates["parent_id"] is None and category.parent_id is not None
    )
    if "color" in updates and updates["color"] is None and ends_as_root:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="a root category must have a color",
        )

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

    if promotes and category.color is None and updates.get("color") is None:
        updates["color"] = next_root_color(
            active_root_colors(session, user_id=user_id, kind=category.kind)
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
    return _reads(session, [category], user_id)[0]


@router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_category(
    category_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
    reassign_to: Annotated[int | None, Query(gt=0)] = None,
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
    if is_fallback_category(category):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="the fallback category cannot be archived",
        )
    # The set this request archives, computed before any stamping.
    subcategories = session.scalars(
        select(Category).where(
            Category.parent_id == category_id,
            Category.user_id == user_id,
            Category.archived_at.is_(None),
        )
    ).all()
    archived_ids = {category_id, *(sub.id for sub in subcategories)}
    if reassign_to is not None:
        if reassign_to not in validate_category_ids(
            session, category_ids=[reassign_to], user_id=user_id, kind=category.kind
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="reassign target not found, archived, or a different kind",
            )
        if reassign_to in archived_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="cannot reassign to a category being archived",
            )
    # naive_utcnow, not utcnow — this is written and later read back /
    # compared (see `archived_at IS NULL` filters app-wide); an aware value
    # would assignment-cast through the server's TimeZone on Postgres v2 and
    # come back shifted (ADR-0001 rule 5).
    now = clock.naive_utcnow()
    category.archived_at = now

    # Archiving a parent archives all its active subcategories with the same stamp,
    # no exceptions (ADR-0012 rule 9). The fallback "Other" is a root that can never
    # be given a parent, so it is never caught here.
    for sub in subcategories:
        sub.archived_at = now

    if reassign_to is not None:
        rows = session.scalars(
            confirmed_only(
                select(Transaction).where(
                    Transaction.user_id == user_id,
                    Transaction.category_id.in_(archived_ids),
                )
            )
        ).all()
        bulk_categorise(
            session,
            user_id=user_id,
            rows=rows,
            category_id=reassign_to,
            resolver=load_alias_resolver(session, user_id=user_id),
        )
        # ADR-0012 rule 10: a linked leg's saved pre-link category is rewritten like a live
        # one. category_id stays NULL on those rows, so the CHECK holds; nothing is taught.
        session.execute(
            update(Transaction)
            .where(
                Transaction.user_id == user_id,
                Transaction.pre_link_category_id.in_(archived_ids),
            )
            .values(pre_link_category_id=reassign_to)
        )

    session.commit()
    return None
