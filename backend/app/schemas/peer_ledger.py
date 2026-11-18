"""Peer ledger request/response schemas (PRD §F3b, ADR-0017).

Auxiliary ledger for peer debts, Khatabook balances, fronted card spending,
and split expenses. All amounts are positive integers in paise (BigInteger > 0).
"""

from __future__ import annotations

from datetime import date as date_t
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.peer_entry import PeerDirectionStr
from app.schemas._common import reject_null_name


class PeerContactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    phone: str | None = Field(default=None, max_length=20)
    upi_id: str | None = Field(default=None, max_length=100)
    notes: str | None = None

    @field_validator("name", mode="after")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("name cannot be blank")
        return s


class PeerContactUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    phone: str | None = Field(default=None, max_length=20)
    upi_id: str | None = Field(default=None, max_length=100)
    notes: str | None = None

    @field_validator("name", mode="after")
    @classmethod
    def _validate_name(cls, v: str | None) -> str | None:
        v = reject_null_name(v)
        s = v.strip()
        if not s:
            raise ValueError("name cannot be blank")
        return s


class PeerContactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    phone: str | None
    upi_id: str | None
    notes: str | None
    archived_at: datetime | None
    created_at: datetime


class PeerContactWithBalanceRead(PeerContactRead):
    balance_paise: int
    total_gave_paise: int
    total_got_paise: int
    active_entries_count: int


class PeerEntryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_id: int
    transaction_id: int | None = None
    date: date_t | None = None
    amount_paise: int = Field(gt=0)
    direction: PeerDirectionStr | None = None
    is_settlement: bool = False
    notes: str | None = None
    original_paise: int | None = None
    discount_paise: int | None = None

    @model_validator(mode="after")
    def _validate_entry(self) -> Self:
        if self.transaction_id is None:
            if self.date is None:
                raise ValueError("date is required for standalone entries")
            if self.direction is None:
                raise ValueError("direction is required for standalone entries")
        if self.original_paise is not None or self.discount_paise is not None:
            if self.original_paise is None or self.discount_paise is None:
                raise ValueError("original_paise and discount_paise must be provided together")
            if self.amount_paise != self.original_paise - self.discount_paise:
                raise ValueError("amount_paise must equal original_paise - discount_paise")
            if not self.notes or not self.notes.strip():
                orig_disp = f"₹{self.original_paise / 100:g}"
                disc_disp = f"₹{self.discount_paise / 100:g}"
                self.notes = f"Original: {orig_disp} less {disc_disp} discount"
        return self


class PeerEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    contact_id: int
    transaction_id: int | None
    date: date_t
    amount_paise: int
    direction: PeerDirectionStr
    is_settlement: bool
    notes: str | None
    created_at: datetime


class PeerSettleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date_t | None = None
    notes: str | None = None


class PeerLedgerSummaryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total_receivable_paise: int
    total_payable_paise: int
    net_balance_paise: int
    active_contacts_count: int
