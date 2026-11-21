"""Rebalancing and target allocation schemas (Milestone 4, PRD §F7/§F8).

Provides:
- AllocationTarget CRUD schemas with basis point validations (10000 = 100.00%)
- Drift solver evaluation schemas (absolute drift, relative drift, Swedroe 5/25 heuristic)
- Rebalancing optimization plan schemas (Mode A water-filling, Mode B tax-aware full rebalance)
"""

from __future__ import annotations

from datetime import date as date_t
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.allocation_target import AllocationScopeTypeStr, AllocationTargetTypeStr
from app.schemas.investments import DecimalStr


class AllocationTargetCreate(BaseModel):
    """POST body to create a single allocation target."""

    scope_type: AllocationScopeTypeStr = "all"
    account_id: int | None = None
    target_type: AllocationTargetTypeStr = "asset_class"
    target_key: str = Field(min_length=1, max_length=64)
    target_bps: int = Field(
        ge=0, le=10000, description="Target weight in basis points (10000 = 100%)"
    )
    rebalance_threshold_bps: int = Field(
        default=500,
        ge=0,
        le=10000,
        description="Rebalancing tolerance threshold in basis points (default 500 = 5.00%)",
    )

    @field_validator("target_key")
    @classmethod
    def normalise_key(cls, v: str) -> str:
        s = v.strip().lower()
        if not s:
            raise ValueError("target_key cannot be blank")
        return s

    @model_validator(mode="after")
    def validate_scope(self) -> AllocationTargetCreate:
        if self.scope_type == "all" and self.account_id is not None:
            raise ValueError("account_id must be null when scope_type='all'")
        if self.scope_type == "account" and self.account_id is None:
            raise ValueError("account_id is required when scope_type='account'")
        return self


class AllocationTargetUpdate(BaseModel):
    """PATCH body to update an allocation target's weight or threshold."""

    target_bps: int | None = Field(
        default=None, ge=0, le=10000, description="Target weight in basis points"
    )
    rebalance_threshold_bps: int | None = Field(
        default=None, ge=0, le=10000, description="Tolerance threshold in basis points"
    )


class AllocationTargetRead(BaseModel):
    """Single allocation target representation."""

    id: int
    scope_type: AllocationScopeTypeStr
    account_id: int | None
    target_type: AllocationTargetTypeStr
    target_key: str
    target_bps: int
    rebalance_threshold_bps: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AllocationTargetBatchCreate(BaseModel):
    """POST body to set/replace targets in a given scope atomically."""

    scope_type: AllocationScopeTypeStr = "all"
    account_id: int | None = None
    targets: list[AllocationTargetCreate]

    @model_validator(mode="after")
    def validate_total_bps(self) -> AllocationTargetBatchCreate:
        total = sum(t.target_bps for t in self.targets)
        if total > 10000:
            raise ValueError(f"Sum of target_bps ({total}) cannot exceed 10000 (100.00%)")
        return self


# --------------------------------------------------------------------------- #
# Drift Evaluation Schemas
# --------------------------------------------------------------------------- #


class RebalanceEvaluateRequest(BaseModel):
    """POST body for /rebalancing/evaluate."""

    scope_type: AllocationScopeTypeStr = "all"
    account_id: int | None = None


class BucketDriftRead(BaseModel):
    """Evaluation result for one allocation bucket."""

    target_id: int | None = None
    target_type: str
    target_key: str
    target_bps: int
    rebalance_threshold_bps: int
    actual_value_paise: int
    target_value_paise: int
    actual_bps: int
    drift_bps: int
    drift_paise: int
    relative_drift_pct: float
    is_out_of_tolerance: bool
    action: Literal["buy", "sell", "hold"]
    holding_count: int


class RebalanceEvaluationRead(BaseModel):
    """Overall portfolio drift evaluation result."""

    scope_type: str
    account_id: int | None
    total_portfolio_value_paise: int
    total_target_bps: int
    is_out_of_tolerance: bool
    buckets: list[BucketDriftRead]
    unallocated_value_paise: int
    unallocated_bps: int


# --------------------------------------------------------------------------- #
# Rebalancing Plan Schemas
# --------------------------------------------------------------------------- #


class RebalancePlanRequest(BaseModel):
    """POST body for /rebalancing/plan."""

    scope_type: AllocationScopeTypeStr = "all"
    account_id: int | None = None
    mode: Literal["inflow_only", "full_rebalance"] = "inflow_only"
    inflow_paise: int = Field(default=0, ge=0, description="Fresh cashflow to deploy in paise")


class RebalancingBucketAction(BaseModel):
    """Summary of cashflow recommended for one allocation bucket."""

    target_id: int | None = None
    target_type: str
    target_key: str
    target_bps: int
    pre_rebalance_value_paise: int
    pre_rebalance_bps: int
    recommended_cashflow_paise: int
    post_rebalance_value_paise: int
    post_rebalance_bps: int
    post_rebalance_drift_bps: int


class LotSellRecommendation(BaseModel):
    """Specific lot recommended for sale in Mode B full rebalance."""

    lot_txn_id: int
    instrument_id: int
    symbol: str
    name: str
    account_id: int
    acquired_on: date_t
    units: DecimalStr
    units_to_sell: DecimalStr
    cost_basis_paise: int
    estimated_proceeds_paise: int
    estimated_gain_paise: int
    holding_period_days: int
    gain_type: Literal["loss", "ltcg", "stcg"]
    is_locked: bool
    lock_expiry_date: date_t | None = None
    tax_drag_rank: int


class RecommendedOrder(BaseModel):
    """Actionable order recommendation."""

    instrument_id: int | None = None
    symbol: str
    name: str
    account_id: int | None = None
    action: Literal["buy", "sell"]
    amount_paise: int
    units: DecimalStr | None = None
    current_nav: DecimalStr | None = None
    reason: str


class RebalancePlanRead(BaseModel):
    """Complete rebalancing optimization plan."""

    mode: Literal["inflow_only", "full_rebalance"]
    scope_type: str
    account_id: int | None
    total_portfolio_value_paise: int
    inflow_paise: int
    post_rebalance_portfolio_value_paise: int
    is_out_of_tolerance: bool
    bucket_actions: list[RebalancingBucketAction]
    recommended_orders: list[RecommendedOrder]
    lot_sell_recommendations: list[LotSellRecommendation]
    summary: str
