"""``/api/v1/tax`` - F11 statement reads (PRD §F11). Read-only; guests may read."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUserId, SessionDep
from app.core import clock
from app.core.fiscal_year import fy_bounds, fy_span, parse_year
from app.schemas import AvailableYearsResponse, IntradayTradeRead, IntradayTradesResponse
from app.services.tax_statement_service import intraday_statement, realised_sale_span

router = APIRouter(prefix="/tax", tags=["tax"])


@router.get("/intraday-trades", response_model=IntradayTradesResponse)
def list_intraday_trades(
    year: str, session: SessionDep, user_id: CurrentUserId
) -> IntradayTradesResponse:
    """Intraday matches whose sell falls in the tax year starting in ``year`` (``YYYY``).

    422 on a malformed year with a generic detail: the input is never echoed.
    """
    fy = parse_year(year)
    if fy is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="year must match YYYY",
        )
    first, last = fy_bounds(fy)
    statement = intraday_statement(session, user_id=user_id, first=first, last=last)
    return IntradayTradesResponse(
        period=str(fy),
        trades=[IntradayTradeRead.model_validate(t) for t in statement.trades],
        total_profit_paise=statement.total_profit_paise,
    )


@router.get("/available-years", response_model=AvailableYearsResponse)
def available_years(session: SessionDep, user_id: CurrentUserId) -> AvailableYearsResponse:
    """Tax years worth offering (FY start years, newest first): every year with a realised
    sale, plus the current one."""
    lo, hi = realised_sale_span(session, user_id=user_id)
    return AvailableYearsResponse(years=fy_span(lo, hi, clock.today()))
