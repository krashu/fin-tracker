"""Label routes (PRD §F3a — user tags).

* ``GET /api/v1/labels`` — list the user's labels, name ASC (autocomplete +
  settings management).
* ``POST /api/v1/labels`` — create. Name is normalized
  (:func:`normalize_label_name`); 409 on duplicate (normalized) name.
* ``PATCH /api/v1/labels/{id}`` — rename. Empty body / rename-to-same is a no-op;
  409 on collision.
* ``DELETE /api/v1/labels/{id}`` — **hard delete**. ``transaction_labels`` links
  are cleared by ``ON DELETE CASCADE`` (PRAGMA foreign_keys=ON), so no manual
  join cleanup here — contrast :mod:`app.api.v1.categories`, which **soft**-deletes
  and deliberately KEEPS its ``merchant_tag_map`` rows (an archived category's
  rules survive and return on un-archive; the ``Category.archived_at IS NULL``
  filter on every read is what stops them being applied).

All queries scope to ``user_id`` from :data:`CurrentUserId`. Cross-dialect 409
detection mirrors :mod:`app.api.v1.categories`.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUserId, SessionDep
from app.models import Label
from app.schemas import LabelCreate, LabelRead, LabelUpdate
from app.services.transaction_labels import _is_label_name_conflict, normalize_label_name

router = APIRouter(prefix="/labels", tags=["labels"])


@router.get("", response_model=list[LabelRead])
def list_labels(
    session: SessionDep,
    user_id: CurrentUserId,
) -> list[Label]:
    stmt = select(Label).where(Label.user_id == user_id).order_by(Label.name.asc())
    return list(session.scalars(stmt))


@router.post("", response_model=LabelRead, status_code=status.HTTP_201_CREATED)
def create_label(
    payload: LabelCreate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> Label:
    name = normalize_label_name(payload.name)
    if name is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="name must not be blank",
        )
    label = Label(user_id=user_id, name=name)
    session.add(label)
    try:
        session.commit()
    except IntegrityError as e:
        session.rollback()
        if _is_label_name_conflict(e.orig):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="label already exists",
            ) from e
        raise
    session.refresh(label)
    return label


@router.patch("/{label_id}", response_model=LabelRead)
def update_label(
    label_id: int,
    payload: LabelUpdate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> Label:
    label = session.scalar(select(Label).where(Label.id == label_id, Label.user_id == user_id))
    if label is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="label not found",
        ) from None
    updates = payload.model_dump(exclude_unset=True)
    if "name" not in updates:
        # Empty body — no DB round-trip, no spurious updated_at bump.
        return label
    name = normalize_label_name(updates["name"])
    if name is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="name must not be blank",
        )
    if name == label.name:
        # Rename to the same normalized name — no-op.
        return label
    label.name = name
    try:
        session.commit()
    except IntegrityError as e:
        session.rollback()
        if _is_label_name_conflict(e.orig):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="label already exists",
            ) from e
        raise
    session.refresh(label)
    return label


@router.delete("/{label_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_label(
    label_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> None:
    """Hard delete. ``transaction_labels`` links cascade at the DB level (the FKs
    are ``ON DELETE CASCADE`` and ``PRAGMA foreign_keys=ON`` is set per
    connection), so a deleted label vanishes from every transaction with no
    manual cleanup. Label has no ORM relationship to its links, so SA issues a
    plain ``DELETE`` and lets the DB cascade run.
    """
    label = session.scalar(select(Label).where(Label.id == label_id, Label.user_id == user_id))
    if label is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="label not found",
        ) from None
    session.delete(label)
    session.commit()
    return None
