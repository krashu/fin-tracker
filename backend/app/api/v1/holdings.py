"""``/api/v1/holdings`` — current investment positions (PRD §F7).

Read-only. ``GET /holdings`` replays every investment transaction through a FIFO
lot queue (``holdings_service.compute_holdings``) and returns one row per
instrument still held. No commit, no business logic in the route — same shape as
the dashboards aggregate routes. The route is unpaginated: the row count is
bounded by the user's distinct instruments (tens), matching the dashboards
rationale.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import CurrentUserId, SessionDep
from app.core import clock
from app.models import InvestmentAccount
from app.schemas import HoldingRead, HoldingsResponse
from app.services.fx_service import latest_rate
from app.services.holdings_service import compute_holdings

router = APIRouter(prefix="/holdings", tags=["holdings"])


@router.get("", response_model=HoldingsResponse)
def list_holdings(
    session: SessionDep,
    user_id: CurrentUserId,
    account_id: Annotated[int | None, Query(gt=0)] = None,
) -> HoldingsResponse:
    if account_id is not None:
        acc = session.scalar(
            select(InvestmentAccount).where(
                InvestmentAccount.id == account_id,
                InvestmentAccount.user_id == user_id,
            )
        )
        if acc is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="investment account not found",
            )
    usd_inr = latest_rate(session)
    holdings = compute_holdings(
        session,
        user_id=user_id,
        usd_inr_rate=usd_inr,
        as_of=clock.today(),
        account_id=account_id,
    )
    return HoldingsResponse(holdings=[HoldingRead.model_validate(h) for h in holdings])
