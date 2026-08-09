"""Account request/response schemas (PRD §F6).

`AccountCreate` is the POST body. The balance-by-type validator enforces:

* ``credit_card`` → ``opening_balance_paise`` must be ``<= 0`` (debt; a
  fresh CC starts owed, not in credit).
* ``bank | cash`` → ``opening_balance_paise`` must be ``>= 0``.
* ``investment`` → must be **exactly 0** (PRD §F6, B#11). An investment
  account is a *placeholder* for grouping, not a store of value: holdings
  live in ``instruments`` / ``investment_transactions``, and
  ``app.api.v1.transactions`` refuses both transactions and transfers on
  the type, so its balance can only ever be its opening balance. Counting
  that alongside ``portfolio_value_paise`` double-counts the same money —
  a ₹50L account plus the CSV import of its own holdings read as ₹1cr.
  Un-itemised balances (PPF/EPF) belong on an *instrument* with a
  manual-NAV ``asset_class`` (``nps`` / ``other``), not here.
  Note this is the one *magnitude* rule among three sign rules.

The rule is one half of a pair: :data:`app.schemas.dashboards.NET_WORTH_EXCLUDED_TYPES`
drops the type from net worth, and this validator is what makes that
lossless rather than lossy. The test
``test_every_account_type_declares_a_net_worth_bucket`` (in
``tests/api/test_accounts.py``) fails if a future type joins one
without the other.

The additive backup-CSV import (``app.services.backup_import_service``) is
deliberately **not** guarded by any of this — it constructs ``Account``
directly, and it resolves a transaction's account by name with no type
check either. Same ruling, and for the same reason, as the CC-issuer gate
at ``app.api.v1.accounts._assert_cc_issuer_or_422``: a restore round-trips
the user's own export (PRD §F10, non-destructive), so rejecting a
historical row would break it. Don't "fix" it into a boundary guard.

``currency`` must be ``INR`` on create: v1 spending is INR-only (PRD §F6)
and investments are account-less (holdings come from
``instruments`` / ``investment_transactions``, not ``Account`` rows), so no
account type has a legitimate non-INR use yet. The column stays USD-capable
for v2 forward-compat — the gate is a validator here, not a DB constraint.

``issuer`` is lowercased on input so the parser dispatch in
``app.services.import_service.PARSERS`` (which keys on lowercase
``"axis"``, ``"icici"``) can find the right parser. A user POSTing
``{"issuer": "Axis"}`` would otherwise create an account that crashes
at upload time with ``LookupError``. For ``credit_card`` accounts the
issuer must additionally be one of ``SUPPORTED_CC_ISSUERS`` — enforced in
the route (``app.api.v1.accounts._assert_cc_issuer_or_422``), not here,
because ``AccountUpdate`` has no ``type`` field and so can't self-validate
the type-conditional rule (same reason ``parent_account_id`` validation is
route-level).

`AccountRead` returns server-managed fields (``archived_at``,
``parent_account_id``) so the UI can render them — /settings/accounts reads
``parent_account_id`` to seed its "Paid from" picker; ``user_id`` is
omitted (single-user v1).

`AccountUpdate` is the PATCH body. Deliberately narrow — only
``name`` / ``issuer`` / ``last4`` / ``parent_account_id`` are writable.
``type`` / ``currency`` / ``opening_balance_paise`` are locked at
creation: mutating ``opening_balance_paise`` silently shifts every
historical dashboard rollup, ``type`` could recategorize CC payments
mid-history, and ``currency`` would invalidate every historical FX
stamp once F7 lands. ``archived_at`` is rejected by ``extra="forbid"``
— ``DELETE /accounts/{id}`` owns archive.

One consequence worth stating, because it decides how the investment
rule above had to be built: an investment account created *before* that
rule keeps its stored balance and there is **no in-app correction path**.
PATCH can't reach the column, and ``DELETE`` is a soft archive that net
worth deliberately still counts (``dashboards.py``'s balances query omits
the ``archived_at`` filter on purpose). So the create-time 422 cannot
repair history — only the net-worth exclusion can, which is why that is
the load-bearing half. Such a row is inert for net worth and still shows
its raw balance in the accounts panel, captioned as not counted.

Cross-row validation for ``parent_account_id`` (must exist, same user,
not archived, type='bank', self type='credit_card', not self) lives in
the route, not on this schema: Pydantic ``model_validator`` cannot
cleanly take a SQLAlchemy session, and the codebase's precedent for
the same problem is :func:`app.api.v1.transactions._assert_category_id_or_422`
— a route-level helper.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.account import AccountTypeStr, CurrencyStr
from app.schemas._common import reject_null_name


class AccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    type: AccountTypeStr
    issuer: str | None = Field(default=None, max_length=64)
    last4: str | None = Field(default=None, min_length=4, max_length=4, pattern=r"^\d{4}$")
    opening_balance_paise: int = 0
    currency: CurrencyStr = "INR"

    @field_validator("issuer", mode="after")
    @classmethod
    def _lowercase_issuer(cls, value: str | None) -> str | None:
        return value.lower() if value is not None else None

    @model_validator(mode="after")
    def _check_balance_by_type(self) -> AccountCreate:
        if self.type == "credit_card" and self.opening_balance_paise > 0:
            raise ValueError("credit_card opening_balance_paise must be <= 0 (debt is negative)")
        if self.type in ("bank", "cash") and self.opening_balance_paise < 0:
            raise ValueError(f"{self.type} opening_balance_paise must be >= 0")
        # A magnitude rule, not a sign rule — hence the rename off "_check_balance_sign".
        if self.type == "investment" and self.opening_balance_paise != 0:
            raise ValueError(
                "investment opening_balance_paise must be 0 "
                "(investment accounts are placeholders; holdings carry the value)"
            )
        return self

    @model_validator(mode="after")
    def _reject_non_inr_currency(self) -> AccountCreate:
        # v1 is INR-only (see module docstring). AccountUpdate needs no mirror:
        # it has no `currency` field and extra="forbid" rejects the key at parse.
        if self.currency != "INR":
            raise ValueError("v1 accounts must be INR (spending is INR-only)")
        return self


class AccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    type: AccountTypeStr
    issuer: str | None
    last4: str | None
    opening_balance_paise: int
    currency: CurrencyStr
    parent_account_id: int | None
    archived_at: datetime | None


class AccountUpdate(BaseModel):
    """Partial-update body for ``PATCH /accounts/{id}`` (PRD §F6).

    Only fields present in the JSON body are written. ``extra="forbid"``
    rejects unknown keys (including the deliberately-locked ``type`` /
    ``currency`` / ``opening_balance_paise`` and the archive-flag
    ``archived_at``) with 422 so an attempted edit surfaces immediately
    rather than silently no-op-ing. Use ``model_dump(exclude_unset=True)``
    in the route to honour the "omitted = leave alone" rule.

    Explicit ``null`` is meaningful for ``parent_account_id`` only —
    unlinks a previously-set CC↔bank parent relationship. ``null`` is
    rejected for ``name`` (NOT NULL column) by :meth:`_reject_null_name`
    below. Note the field IS ``str | None`` — that is what makes
    "omitted" expressible under ``exclude_unset``, so Pydantic's type
    system does NOT reject an explicit null here and the validator is
    load-bearing, not decoration.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    issuer: str | None = Field(default=None, max_length=64)
    last4: str | None = Field(default=None, min_length=4, max_length=4, pattern=r"^\d{4}$")
    parent_account_id: int | None = Field(default=None, gt=0)

    @field_validator("name", mode="after")
    @classmethod
    def _reject_null_name(cls, v: str | None) -> str | None:
        return reject_null_name(v)

    @field_validator("issuer", mode="after")
    @classmethod
    def _lowercase_issuer(cls, value: str | None) -> str | None:
        # Mirrors AccountCreate._lowercase_issuer so the parser dispatch
        # in app.services.import_service.PARSERS keeps working after a
        # rename. Same rule, both endpoints.
        return value.lower() if value is not None else None
