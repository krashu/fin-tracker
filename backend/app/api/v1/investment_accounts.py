"""Investment accounts API routes (ADR-0019).

Enables segmenting portfolios across brokers, depositories, mutual fund folios,
and manual investment buckets.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import CurrentUserId, SessionDep
from app.schemas.investment_account import (
    InvestmentAccountCreate,
    InvestmentAccountRead,
    InvestmentAccountUpdate,
)
from app.services.investment_account_service import (
    create_investment_account,
    delete_investment_account,
    get_investment_account,
    list_investment_accounts,
    update_investment_account,
)

router = APIRouter(prefix="/investment-accounts", tags=["investment-accounts"])


@router.get("", response_model=list[InvestmentAccountRead])
def list_accounts(
    session: SessionDep,
    user_id: CurrentUserId,
    is_active: Annotated[bool | None, Query()] = None,
) -> list[InvestmentAccountRead]:
    accounts = list_investment_accounts(session, user_id=user_id, is_active=is_active)
    return [InvestmentAccountRead.model_validate(a) for a in accounts]


@router.post("", response_model=InvestmentAccountRead, status_code=status.HTTP_201_CREATED)
def create_account(
    payload: InvestmentAccountCreate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> InvestmentAccountRead:
    account = create_investment_account(session, user_id=user_id, payload=payload)
    session.commit()
    session.refresh(account)
    return InvestmentAccountRead.model_validate(account)


@router.get("/{account_id}", response_model=InvestmentAccountRead)
def get_account(
    account_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> InvestmentAccountRead:
    account = get_investment_account(session, user_id=user_id, account_id=account_id)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="investment account not found",
        )
    return InvestmentAccountRead.model_validate(account)


@router.patch("/{account_id}", response_model=InvestmentAccountRead)
def update_account(
    account_id: int,
    payload: InvestmentAccountUpdate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> InvestmentAccountRead:
    account = update_investment_account(
        session, user_id=user_id, account_id=account_id, payload=payload
    )
    session.commit()
    session.refresh(account)
    return InvestmentAccountRead.model_validate(account)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    account_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> None:
    delete_investment_account(session, user_id=user_id, account_id=account_id)
    session.commit()
    return None
