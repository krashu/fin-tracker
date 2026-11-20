"""Pydantic schemas for investment accounts (ADR-0019)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.account import CurrencyStr
from app.models.investment_account import InvestmentAccountTypeStr
from app.schemas._common import reject_null_name


class InvestmentAccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    account_type: InvestmentAccountTypeStr
    provider: str = Field(min_length=1, max_length=64)
    external_account_ref: str = Field(default="default", max_length=128)
    currency: CurrencyStr = "INR"
    is_active: bool = True

    @field_validator("name", mode="after")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("name must not be empty or whitespace-only")
        return stripped

    @field_validator("provider", "external_account_ref", mode="after")
    @classmethod
    def _validate_strings(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("must not be empty or whitespace-only")
        return stripped


class InvestmentAccountUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    is_active: bool | None = None

    @field_validator("name", mode="after")
    @classmethod
    def _check_name_not_cleared(cls, v: str | None) -> str | None:
        if v is None:
            return None
        reject_null_name(v)
        stripped = v.strip()
        if not stripped:
            raise ValueError("name must not be whitespace-only")
        return stripped


class InvestmentAccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    account_type: InvestmentAccountTypeStr
    provider: str
    external_account_ref: str
    currency: CurrencyStr
    is_active: bool
    created_at: datetime
    updated_at: datetime
