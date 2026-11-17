"""Shared FastAPI dependencies.

:data:`SessionDep` injects a SQLAlchemy session per request. :data:`CurrentUserId`
resolves the authenticated user's id from the access-token cookie (PRD §Users &
access v2) — 401 when the cookie is missing, malformed, or expired, and also when
the token names a user that no longer exists or a guest past its expiry (ADR-0003
rule 4): a pruned guest's still-valid token must not read an empty tenant or hit
an FK failure on write. That confirmation is a ``users``-by-id identity lookup,
exempt from the user_id predicate rule. Every owned-table router already depends
on ``CurrentUserId``; swapping the resolver from the old fixed-UUID settings read
to this cookie decode is what turned the app multi-user without touching a single
router signature. :data:`CurrentUser` loads the full row for the handful of
handlers that need more than the id (from the session's identity map, so no
second query). :data:`NonGuestUserId` additionally refuses guests (403) on the
file-upload routes (ADR-0003 rule 15).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core import clock
from app.core.db import get_db
from app.core.security import ACCESS_COOKIE_NAME, decode_access_token
from app.models import User

SessionDep = Annotated[Session, Depends(get_db)]

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="not authenticated",
)


def get_current_user_id(request: Request, session: SessionDep) -> UUID:
    """Resolve the active user id from the access-token cookie; 401 otherwise.

    401 too when the user row is gone or is a guest past ``guest_expires_at``.
    """
    token = request.cookies.get(ACCESS_COOKIE_NAME)
    if token is None:
        raise _UNAUTHENTICATED
    user_id = decode_access_token(token)
    if user_id is None:
        raise _UNAUTHENTICATED
    user = session.get(User, user_id)
    if user is None:
        raise _UNAUTHENTICATED
    if (
        user.is_guest
        and user.guest_expires_at is not None
        and user.guest_expires_at <= clock.naive_utcnow()
    ):
        raise _UNAUTHENTICATED
    return user_id


CurrentUserId = Annotated[UUID, Depends(get_current_user_id)]


def get_current_user(session: SessionDep, user_id: CurrentUserId) -> User:
    """Load the authenticated user row. ``get_current_user_id`` already confirmed it
    exists, so this is an identity-map hit; the ``None`` check only narrows the type."""
    user = session.get(User, user_id)
    if user is None:
        raise _UNAUTHENTICATED
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_non_guest_user_id(user: CurrentUser) -> UUID:
    """The authenticated user's id, refusing guests with 403 (ADR-0003 rule 15).

    Guards every file-upload route: a guest sandbox holds sample data only and
    never a real statement, CSV or backup."""
    if user.is_guest:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="guests cannot upload files; use a sample statement",
        )
    return user.id


NonGuestUserId = Annotated[UUID, Depends(get_non_guest_user_id)]
