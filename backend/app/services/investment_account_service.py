"""Investment account operations and queries (ADR-0019).

Tenant-isolated service layer for managing physical/demat investment accounts.
Supports default account resolution for unsegmented/historical imports.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import InvestmentAccount, InvestmentTransaction
from app.schemas.investment_account import InvestmentAccountCreate, InvestmentAccountUpdate


def get_or_create_default_investment_account(
    session: Session, *, user_id: UUID
) -> InvestmentAccount:
    """Return an active account for ``user_id``, creating 'Default Portfolio' if absent."""
    acc = session.scalar(
        select(InvestmentAccount)
        .where(InvestmentAccount.user_id == user_id, InvestmentAccount.is_active.is_(True))
        .order_by(InvestmentAccount.id)
    )
    if acc is not None:
        return acc

    # If user deactivated their default account, reactivate it rather than failing unique constraint
    existing_default = session.scalar(
        select(InvestmentAccount).where(
            InvestmentAccount.user_id == user_id,
            InvestmentAccount.provider == "manual",
            InvestmentAccount.external_account_ref == "default",
        )
    )
    if existing_default is not None:
        existing_default.is_active = True
        session.flush()
        return existing_default

    acc = InvestmentAccount(
        user_id=user_id,
        name="Default Portfolio",
        account_type="manual",
        provider="manual",
        external_account_ref="default",
        currency="INR",
        is_active=True,
    )
    session.add(acc)
    session.flush()
    return acc


def list_investment_accounts(
    session: Session, *, user_id: UUID, is_active: bool | None = None
) -> list[InvestmentAccount]:
    """Return list of investment accounts owned by ``user_id``."""
    stmt = select(InvestmentAccount).where(InvestmentAccount.user_id == user_id)
    if is_active is not None:
        stmt = stmt.where(InvestmentAccount.is_active == is_active)
    return list(
        session.scalars(stmt.order_by(InvestmentAccount.name.asc(), InvestmentAccount.id.asc()))
    )


def get_investment_account(
    session: Session, *, user_id: UUID, account_id: int
) -> InvestmentAccount | None:
    """Return single investment account owned by ``user_id`` or None."""
    return session.scalar(
        select(InvestmentAccount).where(
            InvestmentAccount.id == account_id, InvestmentAccount.user_id == user_id
        )
    )


def create_investment_account(
    session: Session, *, user_id: UUID, payload: InvestmentAccountCreate
) -> InvestmentAccount:
    """Create a new investment account for ``user_id``, checking for unique provider + ref."""
    existing = session.scalar(
        select(InvestmentAccount).where(
            InvestmentAccount.user_id == user_id,
            InvestmentAccount.provider == payload.provider,
            InvestmentAccount.external_account_ref == payload.external_account_ref,
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="an investment account with this provider and reference already exists",
        )

    acc = InvestmentAccount(
        user_id=user_id,
        name=payload.name,
        account_type=payload.account_type,
        provider=payload.provider,
        external_account_ref=payload.external_account_ref,
        currency=payload.currency,
        is_active=payload.is_active,
    )
    session.add(acc)
    try:
        session.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="an investment account with this provider and reference already exists",
        ) from exc
    return acc


def update_investment_account(
    session: Session, *, user_id: UUID, account_id: int, payload: InvestmentAccountUpdate
) -> InvestmentAccount:
    """Update name or is_active on an owned investment account."""
    acc = get_investment_account(session, user_id=user_id, account_id=account_id)
    if acc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="investment account not found",
        )
    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(acc, field, value)
    session.flush()
    return acc


def delete_investment_account(session: Session, *, user_id: UUID, account_id: int) -> None:
    """Delete an unreferenced investment account; raises 422 if transactions exist."""
    acc = get_investment_account(session, user_id=user_id, account_id=account_id)
    if acc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="investment account not found",
        )
    has_txns = (
        session.scalar(
            select(func.count(InvestmentTransaction.id)).where(
                InvestmentTransaction.account_id == account_id,
                InvestmentTransaction.user_id == user_id,
            )
        )
        or 0
    ) > 0
    if has_txns:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "cannot delete an account that contains investment transactions; archive it instead"
            ),
        )
    session.delete(acc)
    session.flush()
