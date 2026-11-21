"""Schemas for portfolio timeseries and valuation history (Milestone 2 Phase 4)."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

ScopeTypeStr = Literal["all", "account", "member"]


class PortfolioTimeseriesPoint(BaseModel):
    """Daily valuation point."""

    date: date
    portfolio_value_paise: int
    invested_capital_paise: int
    twr_index_micros: int
    benchmark_value_paise: int | None = None
    daily_return: float
    net_cashflow_paise: int
    is_invested: bool


class PeriodPerformance(BaseModel):
    """Performance breakdown over a specific time window."""

    period: str  # e.g. "1M", "3M", "6M", "1Y", "3Y", "5Y", "all"
    start_date: date
    end_date: date
    twr_pct: float | None
    twr_annualized: float | None
    xirr: float | None
    benchmark_xirr: float | None
    alpha: float | None
    max_drawdown: float
    volatility_annualized: float | None
    active_days: int


class PortfolioTimeseriesResponse(BaseModel):
    """Response of ``GET /api/v1/portfolio/timeseries``."""

    scope: ScopeTypeStr
    account_id: int | None = None
    start_date: date
    end_date: date
    benchmark_id: int | None = None
    benchmark_name: str | None = None
    points: list[PortfolioTimeseriesPoint] = Field(default_factory=list)
    periods: list[PeriodPerformance] = Field(default_factory=list)
