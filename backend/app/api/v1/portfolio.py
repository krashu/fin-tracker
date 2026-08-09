"""``/api/v1/portfolio`` — portfolio summary + benchmark performance (PRD §F8 views 5/6).

Read-only aggregates: current value / invested / unrealized P&L, asset-class allocation,
money-weighted return (XIRR), and the scalar "am I beating the market" alpha vs a chosen
index fund. Computation lives in :mod:`app.services.portfolio_service` /
:mod:`app.services.performance_service`; the routes stamp the valuation date (today) and
delegate. The performance read touches only the cached ``benchmark_nav`` — never mfapi.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUserId, SessionDep
from app.core import clock
from app.models import Benchmark
from app.schemas import PortfolioPerformance, PortfolioSummary
from app.services.performance_service import compute_portfolio_performance
from app.services.portfolio_service import compute_portfolio_summary

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("/summary", response_model=PortfolioSummary)
def portfolio_summary(session: SessionDep, user_id: CurrentUserId) -> PortfolioSummary:
    """Portfolio rollup valued as of today (PRD §F8 view 6)."""
    return compute_portfolio_summary(session, user_id=user_id, as_of=clock.today())


@router.get("/performance", response_model=PortfolioPerformance)
def portfolio_performance(
    session: SessionDep,
    user_id: CurrentUserId,
    benchmark_id: int | None = None,
) -> PortfolioPerformance:
    """Scalar alpha vs a benchmark index fund (PRD §F8 view 5).

    ``benchmark_id`` defaults to the first catalog benchmark (Nifty 50). Unknown /
    archived ids → 404. The number is computed from the F7 cashflows + the cached
    ``benchmark_nav`` — no network on this path.
    """
    stmt = select(Benchmark).where(Benchmark.archived_at.is_(None))
    if benchmark_id is not None:
        benchmark = session.scalar(stmt.where(Benchmark.id == benchmark_id))
    else:
        benchmark = session.scalar(stmt.order_by(Benchmark.id.asc()))
    if benchmark is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="benchmark not found")
    return compute_portfolio_performance(
        session, user_id=user_id, benchmark_id=benchmark.id, as_of=clock.today()
    )
