"""Account routes (PRD §F6).

* ``POST /api/v1/accounts`` — create an account.
* ``GET /api/v1/accounts`` — list active (non-archived) accounts, name-sorted.
* ``PATCH /api/v1/accounts/{id}`` — rename, set / unlink ``parent_account_id``,
  edit ``issuer`` / ``last4``. ``type`` / ``currency`` /
  ``opening_balance_paise`` are **not** writable (locked at creation —
  see :class:`AccountUpdate` docstring for why).
* ``DELETE /api/v1/accounts/{id}`` — soft-delete (sets ``archived_at``).
  Re-DELETE returns 404 because the loader filters archived rows.
  Transactions stay linked to the archived account — dashboards still
  aggregate them under the preserved name (locked in by
  ``test_archived_account_transactions_stay_in_totals``).

``IntegrityError`` on the partial unique index ``uq_accounts_active_user_name``
surfaces as 409 with a generic message — no name echo, so user-supplied
account labels stay out of the response body and out of future Sentry
events. The explicit constructor (no ``**payload.model_dump()`` splat) is
deliberate: future fields added to ``AccountCreate`` cannot silently flow
into the ORM constructor.

PATCH validation of ``parent_account_id`` lives inline in the route
(not extracted to a helper): single concrete caller today (this PATCH).
``_assert_category_id_or_422`` exists in :mod:`app.api.v1.transactions`
because it has two callers (POST + PATCH). When a second caller of
parent-account validation lands (e.g. a future ``parent_account_id``
field on ``AccountCreate``), extract then.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import CurrentUserId, SessionDep
from app.core import clock
from app.core.db_errors import is_unique_violation
from app.models import Account
from app.schemas import AccountCreate, AccountRead, AccountUpdate
from app.services.import_service import SUPPORTED_CC_ISSUERS

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _assert_cc_issuer_or_422(account_type: str, issuer: str | None) -> None:
    """A ``credit_card`` must carry an issuer we can parse statements for.

    ``issuer`` is already lowercased by the schema validator before this runs,
    so the membership check is exact. Without this guard, a credit card with an
    unsupported / empty issuer creates fine but crashes at statement-upload time
    with ``ParserNotRegisteredError`` (``import_service.PARSERS`` has no entry).

    UI/API-boundary rule only: the additive backup-CSV import
    (``backup_import_service``) is deliberately *not* guarded — it round-trips
    the user's own exported data (PRD §F10, non-destructive), and rejecting a
    historical row would break a restore.
    """
    if account_type == "credit_card" and issuer not in SUPPORTED_CC_ISSUERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"credit_card issuer must be one of: {', '.join(SUPPORTED_CC_ISSUERS)}",
        )


def _is_name_dup(e: IntegrityError) -> bool:
    """409 detection mirror of ``categories._is_name_dup``. Delegates the
    dialect-aware matching to :func:`app.core.db_errors.is_unique_violation`."""
    return is_unique_violation(
        e.orig,
        index_name="uq_accounts_active_user_name",
        columns=["accounts.user_id", "accounts.name"],
    )


@router.post("", response_model=AccountRead, status_code=status.HTTP_201_CREATED)
def create_account(
    payload: AccountCreate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> Account:
    _assert_cc_issuer_or_422(payload.type, payload.issuer)
    account = Account(
        user_id=user_id,
        name=payload.name,
        type=payload.type,
        issuer=payload.issuer,
        last4=payload.last4,
        opening_balance_paise=payload.opening_balance_paise,
        currency=payload.currency,
    )
    session.add(account)
    try:
        session.commit()
    except IntegrityError as e:
        session.rollback()
        # Only translate the partial-unique-name violation. Other integrity
        # failures (a future FK on parent_account_id, an enum CHECK breach)
        # propagate as 500 — surfacing them as 409 would mislead the caller.
        if _is_name_dup(e):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="account name already exists",
            ) from e
        raise
    session.refresh(account)
    return account


@router.get("", response_model=list[AccountRead])
def list_accounts(
    session: SessionDep,
    user_id: CurrentUserId,
) -> list[Account]:
    stmt = (
        select(Account)
        .where(Account.user_id == user_id, Account.archived_at.is_(None))
        .order_by(Account.name.asc())
    )
    return list(session.scalars(stmt))


@router.patch("/{account_id}", response_model=AccountRead)
def update_account(
    account_id: int,
    payload: AccountUpdate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> Account:
    account = session.scalar(
        select(Account).where(
            Account.id == account_id,
            Account.user_id == user_id,
            Account.archived_at.is_(None),
        )
    )
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="account not found",
        ) from None
    updates = payload.model_dump(exclude_unset=True)
    # Run the issuer guard on the *effective* issuer (incoming value or the
    # stored one) BEFORE the early-returns below: a re-set of the same bad value
    # (PATCH {"issuer":"hdfc"} on a legacy broken CC) would otherwise hit the
    # idempotency short-circuit and return 200 without validation. `type` is
    # immutable so account.type is the effective type. Side effect: a legacy CC
    # with a null/unsupported stored issuer can't have any field patched (even a
    # rename) without supplying a valid issuer — the guard 422s on the unchanged
    # stored value. Intended: it pushes a broken account toward repair.
    _assert_cc_issuer_or_422(account.type, updates.get("issuer", account.issuer))
    if not updates:
        # Empty body — no DB round-trip, no spurious updated_at bump.
        return account
    # Idempotency short-circuit: every supplied field already equals the
    # current value → no-op. Mirrors categories.py's same-name short-circuit.
    if all(getattr(account, k) == v for k, v in updates.items()):
        return account

    if "parent_account_id" in updates and updates["parent_account_id"] is not None:
        _assert_parent_account_or_422(
            session,
            self_id=account_id,
            self_type=account.type,
            parent_id=updates["parent_account_id"],
            user_id=user_id,
        )

    for field, value in updates.items():
        setattr(account, field, value)
    try:
        session.commit()
    except IntegrityError as e:
        session.rollback()
        if _is_name_dup(e):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="account name already exists",
            ) from e
        raise
    session.refresh(account)
    return account


def _assert_parent_account_or_422(
    session: Session,
    *,
    self_id: int,
    self_type: str,
    parent_id: int,
    user_id: UUID,
) -> None:
    """Five-rule validation for ``parent_account_id`` (PRD §F4a-1 link gate).

    Single caller today (PATCH). Inline-near-the-route rather than in
    ``app.services`` because the rules are HTTP-shaped (each failure maps
    to a specific 422 detail) and have no service-layer reuse yet — see
    module docstring §"PATCH validation lives inline".
    """
    if parent_id == self_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="parent_account_id cannot reference self",
        )
    # Per PRD §F4a rule 1: "rule activates only when a CC account has been
    # associated with a parent bank account" — only credit_card accounts
    # carry a parent at all. Surface this before the parent lookup so the
    # error is the actionable one (wrong self type) rather than a generic
    # "parent not found".
    if self_type != "credit_card":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="only credit_card accounts can have a parent",
        )
    parent = session.scalar(
        select(Account).where(
            Account.id == parent_id,
            Account.user_id == user_id,
            Account.archived_at.is_(None),
        )
    )
    if parent is None:
        # Folds three failure modes — unknown / cross-user / archived —
        # into one message. Cross-user must be caught here (the FK alone
        # only proves the row exists; not who owns it).
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="parent account not found or archived",
        )
    if parent.type != "bank":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="parent account must be a bank account",
        )


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    account_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> None:
    """Soft-delete: set ``archived_at = now()``. Idempotent via the
    ``archived_at IS NULL`` loader filter — a 2nd DELETE returns 404.

    No side-effect cleanup — and none is owed: accounts have no learned-rule
    fan-out at all (F3 keys merchant→category, not merchant→account). Nor do
    categories clean up on archive; they deliberately keep their
    ``merchant_tag_map`` rows. Transactions stay linked to the archived
    account — dashboards still aggregate under the preserved name.
    """
    account = session.scalar(
        select(Account).where(
            Account.id == account_id,
            Account.user_id == user_id,
            Account.archived_at.is_(None),
        )
    )
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="account not found",
        ) from None
    account.archived_at = clock.utcnow()
    session.commit()
    return None
