"""Auth request/response schemas (PRD §Users & access v2).

Email validation is intentionally light (non-empty, single ``@``, no spaces)
rather than pulling in ``email-validator`` for pydantic's ``EmailStr`` — a full
RFC validator is more dependency + false-rejection surface than a personal
tracker's signup needs. The service lower-cases/strips before storage.

Tokens are **not** in any response body — they're set as httpOnly cookies by
the router, never handed to JS.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=256)
    display_name: str | None = Field(default=None, max_length=128)

    @field_validator("email", mode="after")
    @classmethod
    def _shape(cls, value: str) -> str:
        v = value.strip()
        if v.count("@") != 1 or v.startswith("@") or v.endswith("@") or " " in v:
            raise ValueError("invalid email")
        return v


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=256)


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # current_password only needs to be non-empty (it's checked against the stored
    # hash, not re-validated for strength); new_password mirrors RegisterRequest's
    # 8-char floor.
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class AuthConfig(BaseModel):
    """Public, pre-auth client config (the login page reads it). Exposes only the
    resolved product policy — whether the shared demo login would actually succeed,
    whether a guest sandbox can be started (``POST /auth/demo-session``), and whether
    ``POST /auth/register`` would currently be accepted (always true until the first
    real user registers, ADR-0003 rule 12)."""

    demo_login_enabled: bool
    guest_sandbox_enabled: bool
    registration_enabled: bool = True


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str | None
    display_name: str | None
    is_guest: bool = False
    # Server-computed (`auth_service.can_manage_credentials`): the client shows a notice instead
    # of the change-password and delete forms when false. Never re-derived from `is_guest` or
    # the email.
    can_manage_credentials: bool


# The typed confirmation phrase for POST /auth/delete-account (ADR-0003 rule 13b); the
# frontend mirrors it.
DELETE_ACCOUNT_PHRASE = "delete my account"


class DeleteAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Checked against the stored hash, not re-validated for strength (as ChangePasswordRequest).
    password: str = Field(min_length=1, max_length=256)
    confirmation: Literal["delete my account"]
