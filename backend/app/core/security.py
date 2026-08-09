"""Auth security primitives (PRD §Users & access v2).

Three concerns, all stateless helpers (no DB, no request):

* **Password hashing** — argon2id via a shared :class:`PasswordHasher`.
  :func:`hash_password` / :func:`verify_password`. Verify swallows argon2's
  ``VerifyMismatchError`` into a bool so callers branch on truthiness, never
  on exceptions.
* **Access tokens** — short-lived HS256 JWTs. :func:`create_access_token`
  encodes ``sub`` (user id) + ``exp``; :func:`decode_access_token` returns the
  user id or ``None`` on any failure (expired / bad signature / malformed).
  Non-raising by design so the log middleware can best-effort resolve a user
  without turning a bad cookie into a 500.
* **Refresh tokens** — opaque random strings. :func:`generate_refresh_token`
  mints one; :func:`hash_refresh_token` sha256s it for storage/lookup (the raw
  token lives only in the user's cookie).

Time comes from :func:`app.core.clock.utcnow` (aware UTC), so ``monkeypatch.setattr(clock,
"utcnow", ...)`` freezes the JWT clock and the session clock together. This module used to own
a private ``now()`` seam that only moved the former.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core import clock
from app.core.config import get_settings

_ALGORITHM = "HS256"
_ph = PasswordHasher()

# Cookie names (shared by the auth router that sets them and deps that read
# them). The refresh cookie is path-scoped to the auth router so it never rides
# non-auth requests — see app/api/v1/auth.py.
ACCESS_COOKIE_NAME = "access_token"
REFRESH_COOKIE_NAME = "refresh_token"


# --- passwords ---------------------------------------------------------------
def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        # VerificationError (parent of VerifyMismatchError) = wrong password;
        # InvalidHashError = a malformed/corrupt stored hash. Both are a failed
        # verification, not a 500 — callers branch on the bool.
        return False


# --- access tokens (JWT) -----------------------------------------------------
def create_access_token(user_id: UUID) -> str:
    settings = get_settings()
    issued = clock.utcnow()
    payload = {
        "sub": str(user_id),
        "iat": int(issued.timestamp()),
        "exp": int((issued + timedelta(minutes=settings.access_token_ttl_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGORITHM)


def decode_access_token(token: str) -> UUID | None:
    """Return the token's user id, or ``None`` on any decode/validation failure."""
    try:
        payload = jwt.decode(token, get_settings().jwt_secret, algorithms=[_ALGORITHM])
        return UUID(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError):
        return None


# --- refresh tokens (opaque) -------------------------------------------------
def generate_refresh_token() -> str:
    """A high-entropy opaque token. Never stored raw — see :func:`hash_refresh_token`."""
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    """sha256 hex of the raw refresh token — the ``sessions.token_hash`` value."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
