"""Net Worth API routes (PRD §F8 View 4 & Wealth Analytics).

Provides:
- GET /api/v1/net-worth/summary: Scalar net worth headline, 30-day/1-year deltas, assets vs debt.
- GET /api/v1/net-worth/timeline: Historical timeseries with category breakdown and decimation.
- GET /api/v1/net-worth/cashflow-waterfall: Year-scoped cashflow waterfall data.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUserId, SessionDep
from app.core import clock
from app.schemas.net_worth import (
    CashflowWaterfallResponse,
    NetWorthSummaryResponse,
    NetWorthTimelineResponse,
    TimelineRangeStr,
    YearTypeStr,
)
from app.services.net_worth_service import (
    compute_cashflow_waterfall,
    compute_net_worth_summary,
    compute_net_worth_timeline,
)

router = APIRouter(prefix="/net-worth", tags=["net-worth"])


@router.get("/summary", response_model=NetWorthSummaryResponse)
def get_net_worth_summary(
    session: SessionDep,
    user_id: CurrentUserId,
    as_of: Annotated[
        date | None, Query(description="Valuation date (defaults to today's IST business date)")
    ] = None,
) -> NetWorthSummaryResponse:
    """Return headline net worth, 30-day delta, 1-year delta, and asset vs liability breakdown."""
    return compute_net_worth_summary(session, user_id=user_id, as_of=as_of)


@router.get("/timeline", response_model=NetWorthTimelineResponse)
def get_net_worth_timeline(
    session: SessionDep,
    user_id: CurrentUserId,
    range: Annotated[
        TimelineRangeStr, Query(alias="range", description="Range filter (1M, 6M, 1Y, 3Y, 5Y, ALL)")
    ] = "1Y",
    as_of: Annotated[
        date | None, Query(description="Terminal date (defaults to today's IST business date)")
    ] = None,
) -> NetWorthTimelineResponse:
    """Return historical net worth timeseries points with breakdown and decimation."""
    return compute_net_worth_timeline(session, user_id=user_id, range_str=range, as_of=as_of)


@router.get("/cashflow-waterfall", response_model=CashflowWaterfallResponse)
def get_net_worth_cashflow_waterfall(
    session: SessionDep,
    user_id: CurrentUserId,
    year: Annotated[
        int | None,
        Query(ge=1900, le=2100, description="Year to analyze (defaults to current year)"),
    ] = None,
    year_type: Annotated[YearTypeStr, Query(description="Calendar or fiscal year")] = "calendar",
) -> CashflowWaterfallResponse:
    """Return annual inflow-to-wealth cashflow waterfall data."""
    target_year = year or clock.today().year
    return compute_cashflow_waterfall(
        session, user_id=user_id, year=target_year, year_type=year_type
    )
