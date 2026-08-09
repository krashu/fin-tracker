"""Shared FastAPI dependencies.

:data:`SessionDep` injects a SQLAlchemy session per request. :data:`CurrentUserId`
resolves the authenticated user's id from the access-token cookie (PRD §Users &
access v2) — 401 when the cookie is missing, malformed, or expired. Every owned-
table router already depends on ``CurrentUserId``; swapping the resolver from the
old fixed-UUID settings read to this cookie decode is what turned the app
multi-user without touching a single router signature. :data:`CurrentUser` loads
the full row for the handful of handlers that need more than the id.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import ACCESS_COOKIE_NAME, decode_access_token
from app.models import User

SessionDep = Annotated[Session, Depends(get_db)]

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="not authenticated",
)


def get_current_user_id(request: Request) -> UUID:
    """Resolve the active user id from the access-token cookie; 401 otherwise."""
    token = request.cookies.get(ACCESS_COOKIE_NAME)
    if token is None:
        raise _UNAUTHENTICATED
    user_id = decode_access_token(token)
    if user_id is None:
        raise _UNAUTHENTICATED
    return user_id


CurrentUserId = Annotated[UUID, Depends(get_current_user_id)]


def get_current_user(session: SessionDep, user_id: CurrentUserId) -> User:
    """Load the authenticated user row; 401 if the token points at a gone user."""
    user = session.get(User, user_id)
    if user is None:
        raise _UNAUTHENTICATED
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
