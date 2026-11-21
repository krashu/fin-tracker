"""``/api/v1/portfolio`` — portfolio summary + benchmark performance (PRD §F8 views 5/6).

Read-only aggregates: current value / invested / unrealized P&L, asset-class allocation,
money-weighted return (XIRR), and the scalar "am I beating the market" alpha vs a chosen
index fund. Computation lives in :mod:`app.services.portfolio_service` /
:mod:`app.services.performance_service`; the routes stamp the valuation date (today) and
delegate. The performance read touches only the cached ``benchmark_nav`` — never mfapi.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUserId, SessionDep
from app.core import clock
from app.models import Benchmark, InvestmentAccount
from app.schemas import (
    PortfolioPerformance,
    PortfolioSummary,
    PortfolioTimeseriesResponse,
    ScopeTypeStr,
)
from app.services.performance_service import compute_portfolio_performance
from app.services.portfolio_analytics_service import get_or_compute_portfolio_timeseries
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


@router.get("/timeseries", response_model=PortfolioTimeseriesResponse)
def portfolio_timeseries(
    session: SessionDep,
    user_id: CurrentUserId,
    scope: ScopeTypeStr = "all",
    account_id: int | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    benchmark_id: int | None = None,
    persist: bool = False,
) -> PortfolioTimeseriesResponse:
    """Forward-sweep valuation and unitized TWR timeseries (PRD §F8/§F9, Milestone 2 Phase 4).

    Returns daily calendar points (portfolio value, invested capital, unitized TWR index,
    counterfactual benchmark value) and period performance summary metrics.
    """
    if from_date and to_date and from_date > to_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="from_date cannot be after to_date",
        )

    if scope == "account" and account_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="account_id is required when scope='account'",
        )

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
                detail=f"investment account {account_id} not found",
            )

    benchmark_key: str | None = "INDEX:NSEI"
    benchmark_name: str | None = "Nifty 50"
    if benchmark_id is not None:
        bench = session.scalar(
            select(Benchmark).where(Benchmark.id == benchmark_id, Benchmark.archived_at.is_(None))
        )
        if bench is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"benchmark {benchmark_id} not found",
            )
        benchmark_name = bench.name
        if bench.kind == "nifty_50":
            benchmark_key = "INDEX:NSEI"
        elif bench.kind == "sp_500":
            benchmark_key = "INDEX:GSPC"
        else:
            benchmark_key = "INDEX:NSEI"
    else:
        first_bench = session.scalar(
            select(Benchmark).where(Benchmark.archived_at.is_(None)).order_by(Benchmark.id.asc())
        )
        if first_bench:
            benchmark_id = first_bench.id
            benchmark_name = first_bench.name

    return get_or_compute_portfolio_timeseries(
        session=session,
        user_id=user_id,
        scope_type=scope,
        scope_id=account_id,
        from_date=from_date,
        to_date=to_date,
        benchmark_key=benchmark_key,
        benchmark_id=benchmark_id,
        benchmark_name=benchmark_name,
        persist=persist,
    )
