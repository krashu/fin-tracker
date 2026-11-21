"""Net Worth Timeline & Cashflow Waterfall schemas (PRD §F8 View 4 & Wealth Analytics).

Provides:
- NetWorthSummaryResponse: scalar net worth headline, 30-day/1-year deltas, assets vs liabilities
- NetWorthTimelinePoint: historical point with asset-category breakdown (bank_cash, indian_equity,
  indian_mf, us_equity, debt_fd, gold, peer_receivables, peer_payables) and net worth total
- NetWorthTimelineResponse: timeseries points with range and decimation metadata
- CashflowWaterfallResponse: annual inflow-to-wealth waterfall steps
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

TimelineRangeStr = Literal["1M", "6M", "1Y", "3Y", "5Y", "ALL"]
TimelineIntervalStr = Literal["daily", "weekly", "monthly"]
YearTypeStr = Literal["calendar", "fiscal"]


class AssetCategoryBreakdown(BaseModel):
    """Breakdown of assets and liabilities across categories in paise."""

    bank_cash_paise: int = Field(
        description="Liquid cash and bank account balances (signed, overdrafts are negative)."
    )
    indian_equity_paise: int = Field(default=0, description="Direct Indian equity holdings value.")
    indian_mf_paise: int = Field(default=0, description="Indian mutual funds holdings value.")
    us_equity_paise: int = Field(default=0, description="US stocks and ETFs holdings value in INR.")
    debt_fd_paise: int = Field(
        default=0, description="Fixed deposits, bonds, and NPS holdings value."
    )
    gold_paise: int = Field(default=0, description="Gold and sovereign gold bonds holdings value.")
    peer_receivables_paise: int = Field(
        default=0, description="Net peer debts owed to user (you_gave > you_got)."
    )
    peer_payables_paise: int = Field(
        default=0,
        description="Net peer debts user owes to others (you_got > you_gave, positive magnitude).",
    )


class NetWorthSummaryResponse(BaseModel):
    """Headline net worth and period deltas (PRD §F8 View 4)."""

    as_of: date
    net_worth_paise: int
    assets_paise: int
    liabilities_paise: int
    delta_30d_paise: int
    delta_30d_pct: float | None = None
    delta_1y_paise: int
    delta_1y_pct: float | None = None
    breakdown: AssetCategoryBreakdown


class NetWorthTimelinePoint(BaseModel):
    """A single date point in the historical net worth timeline."""

    date: date
    net_worth_paise: int
    assets_paise: int
    liabilities_paise: int
    bank_cash_paise: int
    indian_equity_paise: int
    indian_mf_paise: int
    us_equity_paise: int
    debt_fd_paise: int
    gold_paise: int
    peer_receivables_paise: int
    peer_payables_paise: int


class NetWorthTimelineResponse(BaseModel):
    """Envelope for GET /api/v1/net-worth/timeline."""

    range: TimelineRangeStr
    interval: TimelineIntervalStr
    start_date: date
    end_date: date
    points: list[NetWorthTimelinePoint]


class WaterfallCategoryItem(BaseModel):
    """One expense category bar in the waterfall."""

    category_id: int | None
    name: str
    amount_paise: int  # Negative for spend outflow
    display_color: str | None = None


class WaterfallStep(BaseModel):
    """One discrete step in the cashflow waterfall progression."""

    step_index: int
    label: str
    amount_paise: int  # Signed: positive for inflow, negative for outflow
    cumulative_paise: int  # Cumulative level after this step
    kind: Literal["income", "expense", "peer", "investment", "surplus"]
    display_color: str | None = None


class CashflowWaterfallResponse(BaseModel):
    """Envelope for GET /api/v1/dashboards/cashflow-waterfall."""

    year: int
    year_type: YearTypeStr
    start_date: date
    end_date: date
    gross_income_paise: int
    total_living_expenses_paise: int  # Signed negative
    expense_categories: list[WaterfallCategoryItem]
    peer_net_paise: int  # Signed: positive if repayments received, negative if fronted
    investment_transfers_paise: int  # Signed negative
    net_liquid_surplus_paise: int  # Residual liquid cash addition
    steps: list[WaterfallStep]
