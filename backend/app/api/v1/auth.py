"""Auth routes (PRD §Users & access v2).

* ``POST /auth/register`` — create a user + provision default categories; set
  cookies. 409 on a duplicate email.
* ``POST /auth/login`` — verify credentials; set cookies. 401 on bad creds.
* ``POST /auth/refresh`` — rotate the refresh token; reissue the access cookie.
  401 on a missing / unknown / expired / reused token (reuse revokes the family).
* ``POST /auth/logout`` — revoke the refresh session; clear cookies. 204.
* ``POST /auth/change-password`` — verify current pw, re-hash, revoke every session
  and issue a fresh one for the caller (sign-out-everywhere-else). 400 on a wrong /
  unchanged password.
* ``GET /auth/me`` — the authenticated user (id, email, display_name).
* ``GET /auth/config`` — public, pre-auth client config (``demo_login_enabled``).

Tokens are delivered **only** as httpOnly cookies — never in a response body, so
JS can't read them (XSS can't exfiltrate the session). The refresh cookie is
path-scoped to this router so it never rides non-auth requests. register / login
/ refresh are rate-limited (argon2id is expensive → brute-force + DoS surface).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api.deps import CurrentUser, SessionDep
from app.core.config import API_V1_PREFIX, get_settings
from app.core.demo import DEMO_EMAIL
from app.core.rate_limit import RateLimit
from app.core.security import (
    ACCESS_COOKIE_NAME,
    REFRESH_COOKIE_NAME,
    create_access_token,
)
from app.models import User
from app.schemas import (
    AuthConfig,
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    UserRead,
)
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])

# The refresh cookie is only sent to this router's paths (register issues it,
# refresh rotates it, logout clears it). Derived from the single mount-prefix
# source of truth so a re-mount can't silently strand it at a dead path.
_REFRESH_COOKIE_PATH = API_V1_PREFIX + router.prefix


def _set_auth_cookies(response: Response, user: User, refresh_token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        ACCESS_COOKIE_NAME,
        create_access_token(user.id),
        max_age=settings.access_token_ttl_minutes * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,  # ty: ignore[invalid-argument-type]
        path="/",
    )
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        refresh_token,
        max_age=settings.refresh_token_ttl_days * 86400,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,  # ty: ignore[invalid-argument-type]
        path=_REFRESH_COOKIE_PATH,
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE_NAME, path="/")
    response.delete_cookie(REFRESH_COOKIE_NAME, path=_REFRESH_COOKIE_PATH)


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def register(
    payload: RegisterRequest,
    session: SessionDep,
    response: Response,
    _: None = Depends(RateLimit(bucket="register")),
) -> User:
    try:
        user = auth_service.register_user(
            session,
            email=payload.email,
            password=payload.password,
            display_name=payload.display_name,
        )
    except auth_service.EmailAlreadyExistsError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="email already registered",
        ) from e
    refresh_token = auth_service.start_session(session, user.id)
    _set_auth_cookies(response, user, refresh_token)
    return user


@router.post("/login", response_model=UserRead)
def login(
    payload: LoginRequest,
    session: SessionDep,
    response: Response,
    _: None = Depends(RateLimit(bucket="login")),
) -> User:
    user = auth_service.authenticate(session, email=payload.email, password=payload.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid email or password",
        )
    refresh_token = auth_service.start_session(session, user.id)
    _set_auth_cookies(response, user, refresh_token)
    return user


@router.post("/refresh", response_model=UserRead)
def refresh(
    request: Request,
    session: SessionDep,
    response: Response,
    _: None = Depends(RateLimit(bucket="refresh")),
) -> User:
    raw = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="no refresh token")
    rotated = auth_service.rotate_session(session, raw)
    user = session.get(User, rotated.user_id) if rotated is not None else None
    if rotated is None or user is None:
        # Unknown / expired / reused (or user gone). Clear cookies so the client
        # stops retrying.
        _clear_auth_cookies(response)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid refresh token",
        )
    _set_auth_cookies(response, user, rotated.refresh_token)
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, session: SessionDep, response: Response) -> None:
    raw = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw is not None:
        auth_service.revoke_session(session, raw)
    _clear_auth_cookies(response)
    return None


@router.post("/change-password", response_model=UserRead)
def change_password(
    payload: ChangePasswordRequest,
    user: CurrentUser,
    session: SessionDep,
    response: Response,
    _: None = Depends(RateLimit(bucket="change-password")),
) -> User:
    # The demo account's credentials are source-published and shared across
    # visitors — never let a demo session rotate them (it would break "Try the
    # demo" until a reseed). Reachable only where the demo login itself is permitted
    # (Settings.demo_login_permitted — opted in, on plain http), since a session on this
    # account can't be obtained anywhere else.
    if user.email is not None and auth_service.normalize_email(user.email) == DEMO_EMAIL:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the demo account password can't be changed",
        )
    # Reject a no-op change up front — rotating to the same password would revoke
    # every session for zero credential gain.
    if payload.new_password == payload.current_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="new password must differ from the current one",
        )
    new_refresh = auth_service.change_password(
        session,
        user=user,
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    if new_refresh is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="current password is incorrect",
        )
    # Fresh access + refresh cookies keep the acting device logged in; every other
    # device's refresh was just revoked.
    _set_auth_cookies(response, user, new_refresh)
    return user


@router.get("/me", response_model=UserRead)
def me(user: CurrentUser) -> User:
    return user


@router.get("/config", response_model=AuthConfig)
def config() -> AuthConfig:
    # Deliberately reads the SAME expression authenticate() gates the demo login on
    # (Settings.demo_login_permitted) — one source of truth so the "Try the demo" button
    # and the login 401 can never disagree. DEMO_LOGIN_ENABLED is one of its two inputs,
    # not a second gate; see ADR-0003 §Demo account gate for why cookie_secure alone
    # could not close the LAN topology.
    return AuthConfig(demo_login_enabled=get_settings().demo_login_permitted)
