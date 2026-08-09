"""Settings validation guards (PRD §Users & access v2).

These lock the fail-loud checks the credentialed-auth surface needs: a hosted
(``cookie_secure=true``) deploy must not run on the public dev JWT secret, the
CORS allowlist can be neither a wildcard nor empty, and the token TTLs must be
positive. All construct :class:`Settings` with explicit kwargs — init args
outrank the repo-root ``.env`` the top-level conftest loads, so these are
deterministic regardless of local env.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import _DEV_JWT_SECRET, Settings

_VALID_CORS = "http://localhost:3000"


def test_default_jwt_secret_rejected_when_secure() -> None:
    with pytest.raises(ValidationError):
        Settings(cookie_secure=True, jwt_secret=_DEV_JWT_SECRET, cors_allowed_origins=_VALID_CORS)


def test_real_jwt_secret_ok_when_secure() -> None:
    s = Settings(
        cookie_secure=True, jwt_secret="a-real-random-secret", cors_allowed_origins=_VALID_CORS
    )
    assert s.cookie_secure is True


def test_default_secret_ok_when_not_secure() -> None:
    # Local http dev: the placeholder secret is fine while cookie_secure is off.
    s = Settings(cookie_secure=False, jwt_secret=_DEV_JWT_SECRET, cors_allowed_origins=_VALID_CORS)
    assert s.jwt_secret == _DEV_JWT_SECRET


def test_cors_wildcard_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(cors_allowed_origins="*")


def test_cors_empty_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(cors_allowed_origins="")


def test_zero_access_ttl_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(access_token_ttl_minutes=0, cors_allowed_origins=_VALID_CORS)


def test_negative_refresh_ttl_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(refresh_token_ttl_days=-1, cors_allowed_origins=_VALID_CORS)


def test_zero_rate_limit_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(auth_rate_limit_per_minute=0, cors_allowed_origins=_VALID_CORS)
