"""Rebalancing and target allocation service (Milestone 4 Phase 1, PRD §F7/§F8).

Provides:
- AllocationTarget CRUD and validation (sum(target_bps) <= 10000 per scope)
- Drift solver engine: calculates actual weights, drift, and 5/25 Swedroe heuristic
- Rebalancing strategies:
  - Mode A (Inflow-Only / Water-filling): deploys fresh cash with zero sells / tax drag
  - Mode B (Tax-Aware Full Rebalance): lot-level sell recommendations ranked by lowest tax drag
    (loss -> LTCG -> STCG), with statutory 3-year ELSS lock-in guardrail.
"""

from __future__ import annotations

import uuid
from datetime import date as date_t
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core import clock
from app.core.log_config import get_logger
from app.models import (
    AllocationScopeTypeStr,
    AllocationTarget,
    Instrument,
    InvestmentAccount,
)
from app.schemas.rebalancing import (
    AllocationTargetBatchCreate,
    AllocationTargetCreate,
    AllocationTargetUpdate,
    BucketDriftRead,
    LotSellRecommendation,
    RebalanceEvaluationRead,
    RebalancePlanRead,
    RebalancingBucketAction,
    RecommendedOrder,
)
from app.services.fx_math import to_inr_paise
from app.services.fx_service import rate_on
from app.services.holdings_service import (
    Holding,
    OpenLot,
    compute_holdings,
    get_open_lots_by_instrument,
)

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Target Allocation CRUD
# --------------------------------------------------------------------------- #


def list_targets(
    session: Session,
    *,
    user_id: uuid.UUID,
    scope_type: AllocationScopeTypeStr = "all",
    account_id: int | None = None,
) -> list[AllocationTarget]:
    """List allocation targets for user in the given scope."""
    stmt = (
        select(AllocationTarget)
        .where(
            AllocationTarget.user_id == user_id,
            AllocationTarget.scope_type == scope_type,
            AllocationTarget.account_id == account_id,
        )
        .order_by(AllocationTarget.target_type.asc(), AllocationTarget.target_key.asc())
    )
    return list(session.scalars(stmt))


def get_target_or_404(
    session: Session,
    *,
    user_id: uuid.UUID,
    target_id: int,
) -> AllocationTarget:
    """Fetch single target or raise 404 (ADR-0003 tenant isolation)."""
    target = session.scalar(
        select(AllocationTarget).where(
            AllocationTarget.id == target_id,
            AllocationTarget.user_id == user_id,
        )
    )
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="allocation target not found",
        )
    return target


def create_target(
    session: Session,
    *,
    user_id: uuid.UUID,
    target_in: AllocationTargetCreate,
) -> AllocationTarget:
    """Create allocation target with scope, tenant isolation, and bps sum validation."""
    if target_in.scope_type == "account":
        if target_in.account_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="account_id is required when scope_type='account'",
            )
        # Validate investment account belongs to user (422 per ADR-0003 Rule 3)
        acc = session.scalar(
            select(InvestmentAccount).where(
                InvestmentAccount.id == target_in.account_id,
                InvestmentAccount.user_id == user_id,
            )
        )
        if acc is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="investment account not found or does not belong to you",
            )
    else:
        if target_in.account_id is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="account_id must be null when scope_type='all'",
            )

    # Check for existing target with same scope and key
    existing = session.scalar(
        select(AllocationTarget).where(
            AllocationTarget.user_id == user_id,
            AllocationTarget.scope_type == target_in.scope_type,
            AllocationTarget.account_id == target_in.account_id,
            AllocationTarget.target_type == target_in.target_type,
            AllocationTarget.target_key == target_in.target_key,
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"target for {target_in.target_type} '{target_in.target_key}' "
                "already exists in this scope"
            ),
        )

    # Validate sum(target_bps) <= 10000
    current_targets = list_targets(
        session,
        user_id=user_id,
        scope_type=target_in.scope_type,
        account_id=target_in.account_id,
    )
    total_bps = sum(t.target_bps for t in current_targets) + target_in.target_bps
    if total_bps > 10000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"total target allocation ({total_bps} bps) exceeds 10000 bps (100.00%)",
        )

    target = AllocationTarget(
        user_id=user_id,
        scope_type=target_in.scope_type,
        account_id=target_in.account_id,
        target_type=target_in.target_type,
        target_key=target_in.target_key,
        target_bps=target_in.target_bps,
        rebalance_threshold_bps=target_in.rebalance_threshold_bps,
    )
    session.add(target)
    session.flush()
    return target


def update_target(
    session: Session,
    *,
    user_id: uuid.UUID,
    target_id: int,
    target_in: AllocationTargetUpdate,
) -> AllocationTarget:
    """Update allocation target's bps or threshold."""
    target = get_target_or_404(session, user_id=user_id, target_id=target_id)

    if target_in.target_bps is not None:
        current_targets = list_targets(
            session,
            user_id=user_id,
            scope_type=target.scope_type,
            account_id=target.account_id,
        )
        total_bps = (
            sum(t.target_bps for t in current_targets if t.id != target.id) + target_in.target_bps
        )
        if total_bps > 10000:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"total target allocation ({total_bps} bps) exceeds 10000 bps (100.00%)",
            )
        target.target_bps = target_in.target_bps

    if target_in.rebalance_threshold_bps is not None:
        target.rebalance_threshold_bps = target_in.rebalance_threshold_bps

    session.flush()
    return target


def delete_target(
    session: Session,
    *,
    user_id: uuid.UUID,
    target_id: int,
) -> None:
    """Delete single allocation target."""
    target = get_target_or_404(session, user_id=user_id, target_id=target_id)
    session.delete(target)
    session.flush()


def batch_set_targets(
    session: Session,
    *,
    user_id: uuid.UUID,
    batch_in: AllocationTargetBatchCreate,
) -> list[AllocationTarget]:
    """Replace all targets in scope atomically."""
    if batch_in.scope_type == "account":
        if batch_in.account_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="account_id is required when scope_type='account'",
            )
        acc = session.scalar(
            select(InvestmentAccount).where(
                InvestmentAccount.id == batch_in.account_id,
                InvestmentAccount.user_id == user_id,
            )
        )
        if acc is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="investment account not found or does not belong to you",
            )
    else:
        if batch_in.account_id is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="account_id must be null when scope_type='all'",
            )

    total_bps = sum(t.target_bps for t in batch_in.targets)
    if total_bps > 10000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"total target allocation ({total_bps} bps) exceeds 10000 bps (100.00%)",
        )

    # Delete existing targets in this scope
    session.execute(
        delete(AllocationTarget).where(
            AllocationTarget.user_id == user_id,
            AllocationTarget.scope_type == batch_in.scope_type,
            AllocationTarget.account_id == batch_in.account_id,
        )
    )

    created: list[AllocationTarget] = []
    seen_keys: set[tuple[str, str]] = set()
    for t_in in batch_in.targets:
        key_tuple = (t_in.target_type, t_in.target_key)
        if key_tuple in seen_keys:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"duplicate target {t_in.target_type} '{t_in.target_key}' in batch",
            )
        seen_keys.add(key_tuple)

        target = AllocationTarget(
            user_id=user_id,
            scope_type=batch_in.scope_type,
            account_id=batch_in.account_id,
            target_type=t_in.target_type,
            target_key=t_in.target_key,
            target_bps=t_in.target_bps,
            rebalance_threshold_bps=t_in.rebalance_threshold_bps,
        )
        session.add(target)
        created.append(target)

    session.flush()
    return created


# --------------------------------------------------------------------------- #
# Bucket Matching & Drift Solver
# --------------------------------------------------------------------------- #


def _match_holding_to_target(holding: Holding, target: AllocationTarget) -> bool:
    """Match a holding to an allocation target bucket."""
    key = target.target_key.lower().strip()
    target_type = target.target_type

    if target_type == "asset_class":
        h_class = holding.asset_class.lower()
        if h_class == key:
            return True
        if key == "equity" and h_class in ("indian_equity", "indian_mf", "us_equity", "us_etf"):
            return True
        if key == "debt" and h_class in ("bond", "fd", "nps"):
            return True
        return bool(key == "us_equity" and h_class in ("us_equity", "us_etf"))

    if target_type == "instrument":
        return key in (holding.symbol.lower(), str(holding.instrument_id))

    if target_type == "tag":
        return key in holding.symbol.lower() or key in holding.name.lower()

    return False


def evaluate_rebalancing(
    session: Session,
    *,
    user_id: uuid.UUID,
    scope_type: AllocationScopeTypeStr = "all",
    account_id: int | None = None,
    as_of: date_t | None = None,
) -> RebalanceEvaluationRead:
    """Evaluate current portfolio drift and Swedroe 5/25 tolerance status."""
    as_of = as_of or clock.today()
    targets = list_targets(session, user_id=user_id, scope_type=scope_type, account_id=account_id)
    usd_inr = rate_on(session, on=as_of)

    holdings = compute_holdings(
        session,
        user_id=user_id,
        account_id=account_id if scope_type == "account" else None,
        as_of=as_of,
        usd_inr_rate=usd_inr,
    )

    total_portfolio_value_paise = sum(
        h.current_value_inr_paise for h in holdings if h.current_value_inr_paise is not None
    )

    # Map holdings to targets
    bucket_reads: list[BucketDriftRead] = []
    matched_holding_ids: set[int] = set()

    for target in targets:
        matching = [h for h in holdings if _match_holding_to_target(h, target)]
        for h in matching:
            matched_holding_ids.add(h.instrument_id)

        actual_val = sum(
            h.current_value_inr_paise for h in matching if h.current_value_inr_paise is not None
        )
        if total_portfolio_value_paise > 0:
            actual_bps = round((actual_val / total_portfolio_value_paise) * 10000)
            target_val = round(total_portfolio_value_paise * (target.target_bps / 10000))
        else:
            actual_bps = 0
            target_val = 0

        drift_bps = actual_bps - target.target_bps
        drift_paise = actual_val - target_val

        if target_val > 0:
            relative_drift_pct = (abs(actual_val - target_val) / target_val) * 100.0
        else:
            relative_drift_pct = 100.0 if actual_val > 0 else 0.0

        # Swedroe 5/25 Heuristic:
        # Out of tolerance if abs(drift_bps) > rebalance_threshold_bps OR relative_drift_pct > 25.0%
        is_out = (abs(drift_bps) > target.rebalance_threshold_bps) or (relative_drift_pct > 25.0)

        if not is_out:
            action: Literal["buy", "sell", "hold"] = "hold"
        elif drift_bps < 0:
            action = "buy"
        else:
            action = "sell"

        bucket_reads.append(
            BucketDriftRead(
                target_id=target.id,
                target_type=target.target_type,
                target_key=target.target_key,
                target_bps=target.target_bps,
                rebalance_threshold_bps=target.rebalance_threshold_bps,
                actual_value_paise=actual_val,
                target_value_paise=target_val,
                actual_bps=actual_bps,
                drift_bps=drift_bps,
                drift_paise=drift_paise,
                relative_drift_pct=round(relative_drift_pct, 2),
                is_out_of_tolerance=is_out,
                action=action,
                holding_count=len(matching),
            )
        )

    allocated_val = sum(b.actual_value_paise for b in bucket_reads)
    unallocated_val = max(0, total_portfolio_value_paise - allocated_val)
    if total_portfolio_value_paise > 0:
        unallocated_bps = round((unallocated_val / total_portfolio_value_paise) * 10000)
    else:
        unallocated_bps = 0

    total_target_bps = sum(t.target_bps for t in targets)
    is_portfolio_out = any(b.is_out_of_tolerance for b in bucket_reads)

    return RebalanceEvaluationRead(
        scope_type=scope_type,
        account_id=account_id,
        total_portfolio_value_paise=total_portfolio_value_paise,
        total_target_bps=total_target_bps,
        is_out_of_tolerance=is_portfolio_out,
        buckets=bucket_reads,
        unallocated_value_paise=unallocated_val,
        unallocated_bps=unallocated_bps,
    )


# --------------------------------------------------------------------------- #
# Rebalancing Strategies: Water-filling & Full Rebalance
# --------------------------------------------------------------------------- #


def solve_water_filling(
    inflow_paise: int,
    bucket_data: list[tuple[int, int, int]],  # (target_id, current_val_paise, target_bps)
    total_target_bps: int,
) -> dict[int, int]:
    """Solves optimal fresh cash distribution to minimize post-inflow drift.

    Implements continuous water-filling across underweight buckets.
    Ensures zero asset sales and zero tax drag.
    """
    if inflow_paise <= 0 or not bucket_data or total_target_bps <= 0:
        return {b[0]: 0 for b in bucket_data}

    total_current = sum(b[1] for b in bucket_data)
    new_total = total_current + inflow_paise

    # Compute target values and deficits for each bucket
    deficits: dict[int, int] = {}
    for tid, curr_val, tbps in bucket_data:
        ideal_val = (new_total * tbps) // total_target_bps
        deficit = max(0, ideal_val - curr_val)
        deficits[tid] = deficit

    total_deficit = sum(deficits.values())

    # Case 1: No bucket has deficit -> distribute strictly proportional to target_bps
    if total_deficit == 0:
        alloc: dict[int, int] = {}
        allocated = 0
        for tid, _, tbps in bucket_data:
            share = (inflow_paise * tbps) // total_target_bps
            alloc[tid] = share
            allocated += share
        rem = inflow_paise - allocated
        if rem > 0:
            top_tid = max(bucket_data, key=lambda b: b[2])[0]
            alloc[top_tid] += rem
        return alloc

    # Case 2: Inflow >= total_deficit -> top up all deficits completely, distribute rest
    if inflow_paise >= total_deficit:
        alloc = dict(deficits)
        leftover = inflow_paise - total_deficit
        if leftover > 0:
            allocated = 0
            for tid, _, tbps in bucket_data:
                share = (leftover * tbps) // total_target_bps
                alloc[tid] += share
                allocated += share
            rem = leftover - allocated
            if rem > 0:
                top_tid = max(bucket_data, key=lambda b: b[2])[0]
                alloc[top_tid] += rem
        return alloc

    # Case 3: Inflow < total_deficit -> Water-filling among underweight buckets
    # Sort underweight buckets by current ratio (curr_val / ideal_val) ascending
    underweight = []
    for tid, curr_val, tbps in bucket_data:
        ideal_val = (new_total * tbps) // total_target_bps
        if ideal_val > curr_val:
            ratio = curr_val / ideal_val if ideal_val > 0 else 0.0
            underweight.append((tid, curr_val, ideal_val, ratio))

    underweight.sort(key=lambda x: x[3])

    m = 1
    best_theta = 0.0
    for k in range(1, len(underweight) + 1):
        sum_v = sum(underweight[i][1] for i in range(k))
        sum_t = sum(underweight[i][2] for i in range(k))
        if sum_t == 0:
            continue
        theta = (inflow_paise + sum_v) / sum_t
        next_ratio = underweight[k][3] if k < len(underweight) else 1.0
        if theta <= next_ratio or k == len(underweight):
            m = k
            best_theta = theta
            break

    alloc = {b[0]: 0 for b in bucket_data}
    allocated = 0
    for i in range(m):
        tid, curr_val, ideal_val, _ = underweight[i]
        c = max(0, int(best_theta * ideal_val) - curr_val)
        alloc[tid] = c
        allocated += c

    rem = inflow_paise - allocated
    if rem > 0 or rem < 0:
        alloc[underweight[0][0]] += rem

    return alloc


def _check_elss_lock(
    instrument: Instrument, lot: OpenLot, as_of: date_t
) -> tuple[bool, date_t | None]:
    """Check statutory 3-year lock-in guard for ELSS / tax-saving mutual funds."""
    name_l = instrument.name.lower()
    symbol_l = instrument.symbol.lower()
    is_elss = (
        "elss" in name_l or "elss" in symbol_l or "tax saver" in name_l or "tax plan" in name_l
    )
    if not is_elss:
        return False, None

    try:
        lock_expiry = lot.acquired_on.replace(year=lot.acquired_on.year + 3)
    except ValueError:
        lock_expiry = lot.acquired_on.replace(year=lot.acquired_on.year + 3, day=28)

    return as_of < lock_expiry, lock_expiry


def plan_rebalancing(
    session: Session,
    *,
    user_id: uuid.UUID,
    scope_type: AllocationScopeTypeStr = "all",
    account_id: int | None = None,
    mode: Literal["inflow_only", "full_rebalance"] = "inflow_only",
    inflow_paise: int = 0,
    as_of: date_t | None = None,
) -> RebalancePlanRead:
    """Generate intelligent rebalancing plan with Mode A or Mode B optimization."""
    as_of = as_of or clock.today()
    eval_res = evaluate_rebalancing(
        session,
        user_id=user_id,
        scope_type=scope_type,
        account_id=account_id,
        as_of=as_of,
    )

    targets = list_targets(session, user_id=user_id, scope_type=scope_type, account_id=account_id)
    usd_inr = rate_on(session, on=as_of)
    holdings = compute_holdings(
        session,
        user_id=user_id,
        account_id=account_id if scope_type == "account" else None,
        as_of=as_of,
        usd_inr_rate=usd_inr,
    )

    total_val = eval_res.total_portfolio_value_paise
    total_target_bps = eval_res.total_target_bps
    if total_target_bps == 0:
        total_target_bps = 10000

    lot_recommendations: list[LotSellRecommendation] = []
    recommended_orders: list[RecommendedOrder] = []
    recommended_bucket_cashflow: dict[int, int] = {t.id: 0 for t in targets}

    # Fetch open lots and instruments for sell planning
    open_lots_by_inst, inst_by_id = get_open_lots_by_instrument(
        session, user_id=user_id, account_id=account_id if scope_type == "account" else None
    )

    total_realized_proceeds_paise = 0

    if mode == "full_rebalance":
        # Mode B: Solve sell recommendations for overweight buckets
        # Post-rebalance target total portfolio value
        post_target_val = total_val + inflow_paise

        for bucket in eval_res.buckets:
            if bucket.target_id is None:
                continue
            target_obj = next((t for t in targets if t.id == bucket.target_id), None)
            if target_obj is None:
                continue

            target_val_post = (post_target_val * target_obj.target_bps) // total_target_bps
            excess_paise = bucket.actual_value_paise - target_val_post

            if excess_paise > 0 and bucket.is_out_of_tolerance:
                # Need to trim overweight bucket
                bucket_holdings = [h for h in holdings if _match_holding_to_target(h, target_obj)]
                candidate_lots: list[
                    tuple[
                        OpenLot,
                        Instrument,
                        int,
                        int,
                        int,
                        Literal["loss", "ltcg", "stcg"],
                        int,
                        bool,
                        date_t | None,
                    ]
                ] = []

                for h in bucket_holdings:
                    inst = inst_by_id.get(h.instrument_id)
                    if inst is None or inst.current_nav is None or inst.current_nav <= 0:
                        continue
                    lots = open_lots_by_inst.get(inst.id, [])
                    fx_rate = Decimal(1) if inst.currency == "INR" else (usd_inr or Decimal(1))
                    lot_nav_inr = inst.current_nav * fx_rate
                    for lot in lots:
                        is_locked, lock_expiry = _check_elss_lock(inst, lot, as_of)
                        # Proceeds and cost in INR paise
                        proceeds = int(
                            (lot.units * lot_nav_inr * 100).to_integral_value(
                                rounding=ROUND_HALF_EVEN
                            )
                        )
                        cost = to_inr_paise(lot.cost_paise, lot.fx_rate_to_inr)
                        gain = proceeds - cost
                        days = (as_of - lot.acquired_on).days
                        is_foreign = inst.currency != "INR" or inst.asset_class in (
                            "us_equity",
                            "us_etf",
                        )
                        is_debt = inst.asset_class in ("debt", "bank_fd")
                        ltcg_days = 730 if is_foreign else 365

                        if gain < 0:
                            g_type: Literal["loss", "ltcg", "stcg"] = "loss"
                            drag_rank = 1
                        elif is_debt:
                            # Section 50AA: specified debt mutual funds taxed as STCG
                            g_type = "stcg"
                            drag_rank = 3
                        elif days >= ltcg_days:
                            g_type = "ltcg"
                            drag_rank = 2
                        else:
                            g_type = "stcg"
                            drag_rank = 3

                        candidate_lots.append(
                            (
                                lot,
                                inst,
                                proceeds,
                                cost,
                                gain,
                                g_type,
                                drag_rank,
                                is_locked,
                                lock_expiry,
                            )
                        )

                # Rank candidate lots:
                # 1. Unlocked before locked
                # 2. Tax drag rank (1: Loss, 2: LTCG, 3: STCG)
                # 3. For losses: largest loss first (gain ascending)
                # 4. For gains: lowest gain first (gain ascending)
                candidate_lots.sort(key=lambda x: (1 if x[7] else 0, x[6], x[4]))

                remaining_trim = excess_paise
                bucket_sold_paise = 0

                for (
                    lot,
                    inst,
                    proceeds,
                    cost,
                    gain,
                    g_type,
                    drag_rank,
                    is_locked,
                    lock_expiry,
                ) in candidate_lots:
                    if remaining_trim <= 0:
                        break
                    if is_locked:
                        continue  # statutory lock-in guard: skip locked ELSS lots

                    # How much to sell from this lot in INR terms
                    lot_proceeds = proceeds
                    fx_rate = Decimal(1) if inst.currency == "INR" else (usd_inr or Decimal(1))
                    lot_nav_inr = (inst.current_nav or Decimal(1)) * fx_rate
                    if lot_proceeds <= remaining_trim:
                        units_to_sell = lot.units
                        sell_proceeds = lot_proceeds
                        sell_cost = cost
                        sell_gain = gain
                    else:
                        raw_units = Decimal(remaining_trim) / Decimal(100) / lot_nav_inr
                        if inst.asset_class == "indian_equity":
                            units_to_sell = Decimal(int(raw_units))
                            if units_to_sell <= 0:
                                continue
                        else:
                            units_to_sell = raw_units.quantize(
                                Decimal("0.00000001"), rounding=ROUND_HALF_EVEN
                            )
                        if units_to_sell > lot.units:
                            units_to_sell = lot.units
                        sell_proceeds = int(
                            (units_to_sell * lot_nav_inr * 100).to_integral_value(
                                rounding=ROUND_HALF_EVEN
                            )
                        )
                        sell_cost = int(
                            (cost * (units_to_sell / lot.units)).to_integral_value(
                                rounding=ROUND_HALF_EVEN
                            )
                        )
                        sell_gain = sell_proceeds - sell_cost

                    remaining_trim -= sell_proceeds
                    bucket_sold_paise += sell_proceeds
                    total_realized_proceeds_paise += sell_proceeds

                    lot_rec = LotSellRecommendation(
                        lot_txn_id=lot.txn_id,
                        instrument_id=inst.id,
                        symbol=inst.symbol,
                        name=inst.name,
                        account_id=lot.account_id,
                        acquired_on=lot.acquired_on,
                        units=lot.units,
                        units_to_sell=units_to_sell,
                        cost_basis_paise=sell_cost,
                        estimated_proceeds_paise=sell_proceeds,
                        estimated_gain_paise=sell_gain,
                        holding_period_days=(as_of - lot.acquired_on).days,
                        gain_type=g_type,
                        is_locked=is_locked,
                        lock_expiry_date=lock_expiry,
                        tax_drag_rank=drag_rank,
                    )
                    lot_recommendations.append(lot_rec)

                    recommended_orders.append(
                        RecommendedOrder(
                            instrument_id=inst.id,
                            symbol=inst.symbol,
                            name=inst.name,
                            account_id=lot.account_id,
                            action="sell",
                            amount_paise=sell_proceeds,
                            units=units_to_sell,
                            current_nav=inst.current_nav,
                            reason=f"Trim overweight {target_obj.target_key} ({g_type.upper()})",
                        )
                    )

                recommended_bucket_cashflow[target_obj.id] = -bucket_sold_paise

    # Fresh capital to allocate = user inflow + realized proceeds from sales
    available_inflow = inflow_paise + total_realized_proceeds_paise

    # Deploy available_inflow across target buckets using Water-Filling
    # Bucket current values after sell trims:
    bucket_data = [
        (
            t.id,
            max(0, b.actual_value_paise + recommended_bucket_cashflow.get(t.id, 0)),
            t.target_bps,
        )
        for t, b in zip(targets, eval_res.buckets, strict=False)
    ]
    inflow_allocations = solve_water_filling(available_inflow, bucket_data, total_target_bps)

    for tid, cash_in in inflow_allocations.items():
        recommended_bucket_cashflow[tid] = recommended_bucket_cashflow.get(tid, 0) + cash_in

    # Build RebalancingBucketAction list and Buy RecommendedOrders
    bucket_actions: list[RebalancingBucketAction] = []
    post_portfolio_val = total_val + inflow_paise  # net change is exactly inflow_paise

    for target in targets:
        b_drift = next((b for b in eval_res.buckets if b.target_id == target.id), None)
        pre_val = b_drift.actual_value_paise if b_drift else 0
        pre_bps = b_drift.actual_bps if b_drift else 0
        flow = recommended_bucket_cashflow.get(target.id, 0)
        post_val = max(0, pre_val + flow)
        post_bps = round((post_val / post_portfolio_val) * 10000) if post_portfolio_val > 0 else 0
        post_drift = post_bps - target.target_bps

        bucket_actions.append(
            RebalancingBucketAction(
                target_id=target.id,
                target_type=target.target_type,
                target_key=target.target_key,
                target_bps=target.target_bps,
                pre_rebalance_value_paise=pre_val,
                pre_rebalance_bps=pre_bps,
                recommended_cashflow_paise=flow,
                post_rebalance_value_paise=post_val,
                post_rebalance_bps=post_bps,
                post_rebalance_drift_bps=post_drift,
            )
        )

        # Generate buy orders for positive cashflow
        if flow > 0:
            matching_holdings = [h for h in holdings if _match_holding_to_target(h, target)]
            if matching_holdings:
                # Allocate to the first / primary holding or most underweight in bucket
                primary_h = matching_holdings[0]
                nav = primary_h.current_nav or Decimal(1)
                units = (Decimal(flow) / Decimal(100) / nav).quantize(
                    Decimal("0.00000001"), rounding=ROUND_HALF_EVEN
                )
                recommended_orders.append(
                    RecommendedOrder(
                        instrument_id=primary_h.instrument_id,
                        symbol=primary_h.symbol,
                        name=primary_h.name,
                        account_id=primary_h.account_id,
                        action="buy",
                        amount_paise=flow,
                        units=units,
                        current_nav=nav,
                        reason=f"Top up underweight {target.target_key}",
                    )
                )
            else:
                recommended_orders.append(
                    RecommendedOrder(
                        instrument_id=None,
                        symbol=target.target_key.upper(),
                        name=f"New Position: {target.target_key}",
                        account_id=None,
                        action="buy",
                        amount_paise=flow,
                        units=None,
                        current_nav=None,
                        reason=f"Initiate allocation in {target.target_key}",
                    )
                )

    # Executive summary
    underweight_count = sum(1 for a in bucket_actions if a.recommended_cashflow_paise > 0)
    trimmed_count = sum(1 for a in bucket_actions if a.recommended_cashflow_paise < 0)

    if mode == "inflow_only":
        summary = (
            f"Mode A (Inflow-Only): Allocated ₹{inflow_paise / 100:,.2f} across "
            f"{underweight_count} underweight bucket(s) via water-filling. "
            "Zero asset sales, zero tax drag."
        )
    else:
        summary = (
            f"Mode B (Tax-Aware Full Rebalance): Recommended {len(lot_recommendations)} lot sales "
            f"yielding ₹{total_realized_proceeds_paise / 100:,.2f} across "
            f"{trimmed_count} overweight bucket(s) (blocking locked ELSS lots) and deployed "
            f"₹{available_inflow / 100:,.2f} into {underweight_count} underweight bucket(s)."
        )

    return RebalancePlanRead(
        mode=mode,
        scope_type=scope_type,
        account_id=account_id,
        total_portfolio_value_paise=total_val,
        inflow_paise=inflow_paise,
        post_rebalance_portfolio_value_paise=post_portfolio_val,
        is_out_of_tolerance=eval_res.is_out_of_tolerance,
        bucket_actions=bucket_actions,
        recommended_orders=recommended_orders,
        lot_sell_recommendations=lot_recommendations,
        summary=summary,
    )
