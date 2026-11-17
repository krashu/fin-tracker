"""Auth routes (PRD §Users & access v2).

* ``POST /auth/register`` — create a user + provision default categories; set
  cookies. 409 on a duplicate email. 403 once a real user exists unless
  ``REGISTRATION_ENABLED`` (the first user can always register).
* ``POST /auth/login`` — verify credentials; set cookies. 401 on bad creds.
* ``POST /auth/refresh`` — rotate the refresh token; reissue the access cookie.
  401 on a missing / unknown / expired / reused token (reuse revokes the family).
* ``POST /auth/logout`` — revoke the refresh session; clear cookies. 204.
* ``POST /auth/demo-session`` — start a guest sandbox (a fresh, password-less,
  expiring user with synthetic data); set cookies. 403 unless
  ``GUEST_SANDBOX_ENABLED``; 503 at ``MAX_GUEST_ACCOUNTS``.
* ``POST /auth/change-password`` — verify current pw, re-hash, revoke every session
  and issue a fresh one for the caller (sign-out-everywhere-else). 400 on a wrong /
  unchanged password.
* ``POST /auth/delete-account`` — verify the password and the typed phrase, hard-delete the
  caller and everything they own (ADR-0003 rule 13b), clear cookies. 204; 400 on a wrong
  password; 403 for the demo account and guests.
* ``GET /auth/me`` — the authenticated user (id, email, display_name).
* ``GET /auth/config`` — public, pre-auth client config (``demo_login_enabled``,
  ``guest_sandbox_enabled``, ``registration_enabled``).

Tokens are delivered **only** as httpOnly cookies — never in a response body, so
JS can't read them (XSS can't exfiltrate the session). The refresh cookie is
path-scoped to this router so it never rides non-auth requests. register / login
/ refresh / demo-session / change-password / delete-account are rate-limited (argon2id is
expensive → brute-force + DoS surface).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api.deps import CurrentUser, SessionDep
from app.core.config import API_V1_PREFIX, get_settings
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
    DeleteAccountRequest,
    LoginRequest,
    RegisterRequest,
    UserRead,
)
from app.services import auth_service, guest_service

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


def _user_read(user: User) -> UserRead:
    return UserRead(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_guest=user.is_guest,
        can_manage_credentials=auth_service.can_manage_credentials(user),
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
) -> UserRead:
    # ADR-0003 rule 12: the first user can always register; after that the gate
    # decides. The check-then-insert is not atomic: two simultaneous first
    # registrations can both pass, which still yields only the operator's own users.
    if not get_settings().registration_enabled and auth_service.has_registered_user(session):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="registration is disabled",
        )
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
    return _user_read(user)


@router.post("/login", response_model=UserRead)
def login(
    payload: LoginRequest,
    session: SessionDep,
    response: Response,
    _: None = Depends(RateLimit(bucket="login")),
) -> UserRead:
    user = auth_service.authenticate(session, email=payload.email, password=payload.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid email or password",
        )
    refresh_token = auth_service.start_session(session, user.id)
    _set_auth_cookies(response, user, refresh_token)
    return _user_read(user)


@router.post("/refresh", response_model=UserRead)
def refresh(
    request: Request,
    session: SessionDep,
    response: Response,
    _: None = Depends(RateLimit(bucket="refresh")),
) -> UserRead:
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
    return _user_read(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, session: SessionDep, response: Response) -> None:
    raw = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw is not None:
        auth_service.revoke_session(session, raw)
    _clear_auth_cookies(response)
    return None


@router.post("/demo-session", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def demo_session(
    session: SessionDep,
    response: Response,
    _: None = Depends(RateLimit(bucket="demo-session")),
) -> UserRead:
    # Guests have their own gate (ADR-0003 rule 15), independent of the shared demo
    # login's demo_login_permitted: a guest owns only synthetic data, so it may run
    # over HTTPS where the demo login never does.
    if not get_settings().guest_sandbox_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="guest sandboxes are disabled on this instance",
        )
    try:
        user, refresh_token = guest_service.create_guest_sandbox(session)
    except guest_service.GuestCapReachedError as err:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="demo is at capacity, try again later",
        ) from err
    _set_auth_cookies(response, user, refresh_token)
    return _user_read(user)


@router.post("/change-password", response_model=UserRead)
def change_password(
    payload: ChangePasswordRequest,
    user: CurrentUser,
    session: SessionDep,
    response: Response,
    _: None = Depends(RateLimit(bucket="change-password")),
) -> UserRead:
    # The demo account's credentials are source-published and shared across
    # visitors — never let a demo session rotate them (it would break "Try the
    # demo" until a reseed). Reachable only where the demo login itself is permitted
    # (Settings.demo_login_permitted — opted in, on plain http), since a session on this
    # account can't be obtained anywhere else.
    if not auth_service.can_manage_credentials(user):
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
    return _user_read(user)


@router.post("/delete-account", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    payload: DeleteAccountRequest,
    user: CurrentUser,
    session: SessionDep,
    response: Response,
    _: None = Depends(RateLimit(bucket="delete-account")),
) -> None:
    # Demo and guest accounts are shared / ephemeral: refuse before any argon2 work.
    if not auth_service.can_manage_credentials(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the demo account and guests can't be deleted",
        )
    # 400, not 401: the session is valid, so a wrong password is a body error — a 401 would
    # send the client's refresh-and-replay transport into a logout.
    if not auth_service.delete_account(session, user=user, password=payload.password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="current password is incorrect",
        )
    _clear_auth_cookies(response)


@router.get("/me", response_model=UserRead)
def me(user: CurrentUser) -> UserRead:
    return _user_read(user)


@router.get("/config", response_model=AuthConfig)
def config(session: SessionDep) -> AuthConfig:
    # Deliberately reads the SAME expression authenticate() gates the demo login on
    # (Settings.demo_login_permitted) — one source of truth so the "Try the demo" button
    # and the login 401 can never disagree; likewise guest_sandbox_enabled is the flag
    # POST /demo-session checks. registration_enabled is the EFFECTIVE value
    # register applies — open while no real user exists (first-user bootstrap).
    settings = get_settings()
    return AuthConfig(
        demo_login_enabled=settings.demo_login_permitted,
        guest_sandbox_enabled=settings.guest_sandbox_enabled,
        registration_enabled=(
            settings.registration_enabled or not auth_service.has_registered_user(session)
        ),
    )
