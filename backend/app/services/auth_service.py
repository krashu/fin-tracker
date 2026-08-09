"""Auth service — registration, login, refresh-token lifecycle (PRD §Users & access v2).

Pure DB logic; cookie/HTTP concerns live in :mod:`app.api.v1.auth`. Every datetime written
to / compared against ``sessions`` is **naive UTC** via :func:`app.core.clock.naive_utcnow`
— ADR-0001 rule 5. ``created_at`` included, since remediation step 11: the absolute-cap
subtraction below reads it back in Python, so it cannot come from the database server's
clock. The rule and its reasons are owned by that ADR and :mod:`app.core.clock`; don't
restate them per site.

Refresh rotation with reuse detection:

* Login / register mints a *family* (a fresh ``family_id``) and its first token.
* Each refresh revokes the presented row and issues a new one in the same family.
* Presenting an already-revoked token (reuse — a stolen, rotated-past token) is
  treated as compromise: the **entire family** is revoked, killing any session
  an attacker spun off it. The legitimate user must log in again.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any, NamedTuple, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import clock
from app.core.config import get_settings
from app.core.demo import DEMO_EMAIL
from app.core.security import (
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.models import RefreshSession, User
from app.services.provisioning import provision_default_categories

# A throwaway argon2 hash verified against on the user-not-found login path so a
# missing email and a wrong password take the same time (no timing oracle). Minted
# once at import from a random secret — it only needs to be *a* valid hash with the
# app's params; deliberately NOT the demo hash (that coupling was a footgun).
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


class EmailAlreadyExistsError(Exception):
    """Registration with an email that already has an account."""


class RotatedSession(NamedTuple):
    user_id: UUID
    refresh_token: str  # the new raw token (set as the cookie)


def normalize_email(email: str) -> str:
    return email.strip().lower()


# --- registration / login ----------------------------------------------------
def register_user(
    session: Session, *, email: str, password: str, display_name: str | None = None
) -> User:
    """Create a fresh user + provision default categories. Commits.

    Raises :class:`EmailAlreadyExistsError` on a duplicate email (checked
    up-front and again via the partial unique index, which wins any race).
    """
    normalized = normalize_email(email)
    existing = session.scalar(select(User.id).where(User.email == normalized))
    if existing is not None:
        raise EmailAlreadyExistsError

    user = User(
        email=normalized,
        password_hash=hash_password(password),
        display_name=display_name,
    )
    session.add(user)
    try:
        # flush() emits the INSERT — the partial unique index on email is enforced
        # HERE, not at commit(), so it must be inside the try to catch the race
        # (pre-check passed, a concurrent register committed the same email during
        # our hash_password window). The only IntegrityError reachable in this
        # block is that email collision: user.id is a fresh uuid4 and the
        # provisioned categories are unique per (user_id, name, kind). The commit()
        # is here too so a Postgres-v2 deferred constraint surfacing there is caught.
        session.flush()  # also assigns user.id before provisioning FKs against it
        provision_default_categories(session, user.id)
        session.commit()
    except IntegrityError as e:  # lost the unique-index race
        session.rollback()
        raise EmailAlreadyExistsError from e
    session.refresh(user)
    return user


def authenticate(session: Session, *, email: str, password: str) -> User | None:
    """Return the user for valid credentials, else ``None`` (constant-ish time)."""
    normalized = normalize_email(email)
    user = session.scalar(select(User).where(User.email == normalized))
    if user is None or user.password_hash is None:
        verify_password(password, _DUMMY_HASH)  # equalize timing
        return None
    # The demo account ships a source-published password, so it authenticates only where
    # the operator has explicitly opted in AND the transport is plain http
    # (Settings.demo_login_permitted — the same expression GET /auth/config reads, so the
    # button and this 401 can't disagree). Refused by default. Dummy verify keeps timing
    # equal to a normal wrong-password.
    if not get_settings().demo_login_permitted and normalized == normalize_email(DEMO_EMAIL):
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


# --- refresh-token lifecycle -------------------------------------------------
def start_session(session: Session, user_id: UUID) -> str:
    """Mint a new refresh-token family for ``user_id``; return the raw token. Commits."""
    return _issue(session, user_id=user_id, family_id=uuid4(), commit=True)


def rotate_session(session: Session, raw_token: str) -> RotatedSession | None:
    """Validate + rotate a refresh token. Commits.

    ``None`` (caller → 401) when the token is unknown, expired, or reused. On
    reuse of an already-revoked token, the whole family is revoked first.
    """
    row = session.scalar(
        select(RefreshSession).where(RefreshSession.token_hash == hash_refresh_token(raw_token))
    )
    if row is None:
        return None

    if row.revoked_at is not None:
        # Reuse of a rotated/revoked token → compromise. Nuke the family.
        _revoke_family(session, row.family_id)
        session.commit()
        return None

    if row.expires_at <= clock.naive_utcnow():
        row.revoked_at = clock.naive_utcnow()
        session.commit()
        return None

    # Absolute session cap (OWASP absolute timeout). The sliding refresh TTL above
    # bounds *idle* lifetime; this bounds *total* lifetime from login, so a stolen /
    # replayed refresh token can't be rotated forever. Origin = the family's
    # first-issued row: rotation copies family_id but each row gets a fresh
    # created_at, so MIN(created_at) stays pinned to the login/mint time.
    # created_at is written naive-UTC by the app clock on every dialect (ADR-0001 rule 5),
    # so the subtraction is correct by construction rather than by SQLite coincidence.
    # Past the cap → revoke the family (same kill-and-401 path as reuse/expiry).
    origin = session.scalar(
        select(func.min(RefreshSession.created_at)).where(RefreshSession.family_id == row.family_id)
    )
    if origin is not None and clock.naive_utcnow() - origin >= timedelta(
        hours=get_settings().session_absolute_ttl_hours
    ):
        _revoke_family(session, row.family_id)
        session.commit()
        return None

    # Rotate atomically: revoke via a conditional UPDATE and treat a 0-rowcount
    # as "already rotated" (a concurrent refresh won the race). Without this, two
    # in-flight refreshes of the same live token — an ordinary SPA double-submit
    # on expiry — could both pass the Python-level check and mint two live
    # successors, defeating single-use rotation.
    # DML execute returns a CursorResult (typed as Result) — cast for .rowcount.
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(RefreshSession)
            .where(RefreshSession.id == row.id, RefreshSession.revoked_at.is_(None))
            .values(revoked_at=clock.naive_utcnow())
        ),
    )
    if result.rowcount != 1:
        session.rollback()
        return None
    new_token = _issue(session, user_id=row.user_id, family_id=row.family_id, commit=False)
    session.commit()
    return RotatedSession(user_id=row.user_id, refresh_token=new_token)


def change_password(
    session: Session, *, user: User, current_password: str, new_password: str
) -> str | None:
    """Re-hash the password, kill every session, mint a fresh one. Commits.

    Returns the new raw refresh token (the caller sets it as the cookie so the
    acting device stays logged in), or ``None`` when ``current_password`` doesn't
    match (caller → 400). Every refresh family for the user — the caller's current
    one included — is revoked; the acting device continues on the freshly-issued
    token, every *other* device's refresh dies. (Already-issued access JWTs aren't
    revocable, so other devices linger until their ~15-min access token expires,
    then 401 on refresh — see :func:`app.api.deps.get_current_user_id`.)
    """
    if user.password_hash is None or not verify_password(current_password, user.password_hash):
        return None
    user.password_hash = hash_password(new_password)
    # NOTE (Postgres v2 caveat): a concurrent login could INSERT a live family in the
    # gap between this revoke-all and the mint below, leaving >1 live family. Harmless
    # on SQLite single-box v1 (and it'd take a concurrent login mid-self-password-change);
    # revisit with a session-epoch if v2 ever needs strict single-family-after-change.
    _revoke_all_for_user(session, user.id)
    new_token = _issue(session, user_id=user.id, family_id=uuid4(), commit=False)
    session.commit()
    return new_token


def revoke_session(session: Session, raw_token: str) -> None:
    """Logout: revoke the presented token's row if live. Idempotent. Commits."""
    row = session.scalar(
        select(RefreshSession).where(RefreshSession.token_hash == hash_refresh_token(raw_token))
    )
    if row is not None and row.revoked_at is None:
        row.revoked_at = clock.naive_utcnow()
    session.commit()


def _issue(session: Session, *, user_id: UUID, family_id: UUID, commit: bool) -> str:
    raw = generate_refresh_token()
    expires = clock.naive_utcnow() + timedelta(days=get_settings().refresh_token_ttl_days)
    session.add(
        RefreshSession(
            user_id=user_id,
            family_id=family_id,
            token_hash=hash_refresh_token(raw),
            expires_at=expires,
        )
    )
    if commit:
        session.commit()
    return raw


def _revoke_family(session: Session, family_id: UUID) -> None:
    session.execute(
        update(RefreshSession)
        .where(RefreshSession.family_id == family_id, RefreshSession.revoked_at.is_(None))
        .values(revoked_at=clock.naive_utcnow())
    )


def _revoke_all_for_user(session: Session, user_id: UUID) -> None:
    """Revoke every live refresh session for the user (all families). Used by
    change-password to log out all devices before issuing a fresh session."""
    session.execute(
        update(RefreshSession)
        .where(RefreshSession.user_id == user_id, RefreshSession.revoked_at.is_(None))
        .values(revoked_at=clock.naive_utcnow())
    )
