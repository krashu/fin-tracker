/**
 * Rebalancing & Target Allocation API client (Milestone 4, PRD §F7/§F8).
 *
 * Types are hand-mirrored from backend Pydantic models in
 * `backend/app/schemas/rebalancing.py`.
 */

import { request } from "@/lib/api/client";

export type AllocationScopeType = "all" | "account";
export type AllocationTargetType = "asset_class" | "instrument" | "tag";

export type AllocationTargetCreate = {
  scope_type?: AllocationScopeType;
  account_id?: number | null;
  target_type?: AllocationTargetType;
  target_key: string;
  target_bps: number; // 0..10000 (10000 = 100.00%)
  rebalance_threshold_bps?: number; // default 500 (5.00%)
};

export type AllocationTargetUpdate = {
  target_bps?: number | null;
  rebalance_threshold_bps?: number | null;
};

export type AllocationTargetRead = {
  id: number;
  scope_type: AllocationScopeType;
  account_id: number | null;
  target_type: AllocationTargetType;
  target_key: string;
  target_bps: number;
  rebalance_threshold_bps: number;
  created_at: string;
  updated_at: string;
};

export type AllocationTargetBatchCreate = {
  scope_type?: AllocationScopeType;
  account_id?: number | null;
  targets: AllocationTargetCreate[];
};

export type RebalanceEvaluateRequest = {
  scope_type?: AllocationScopeType;
  account_id?: number | null;
};

export type BucketDriftRead = {
  target_id: number | null;
  target_type: string;
  target_key: string;
  target_bps: number;
  rebalance_threshold_bps: number;
  actual_value_paise: number;
  target_value_paise: number;
  actual_bps: number;
  drift_bps: number;
  drift_paise: number;
  relative_drift_pct: number;
  is_out_of_tolerance: boolean;
  action: "buy" | "sell" | "hold";
  holding_count: number;
};

export type RebalanceEvaluationRead = {
  scope_type: string;
  account_id: number | null;
  total_portfolio_value_paise: number;
  total_target_bps: number;
  is_out_of_tolerance: boolean;
  buckets: BucketDriftRead[];
  unallocated_value_paise: number;
  unallocated_bps: number;
};

export type RebalancePlanRequest = {
  scope_type?: AllocationScopeType;
  account_id?: number | null;
  mode?: "inflow_only" | "full_rebalance";
  inflow_paise?: number;
};

export type RebalancingBucketAction = {
  target_id: number | null;
  target_type: string;
  target_key: string;
  target_bps: number;
  pre_rebalance_value_paise: number;
  pre_rebalance_bps: number;
  recommended_cashflow_paise: number;
  post_rebalance_value_paise: number;
  post_rebalance_bps: number;
  post_rebalance_drift_bps: number;
};

export type LotSellRecommendation = {
  lot_txn_id: number;
  instrument_id: number;
  symbol: string;
  name: string;
  account_id: number;
  acquired_on: string;
  units: string;
  units_to_sell: string;
  cost_basis_paise: number;
  estimated_proceeds_paise: number;
  estimated_gain_paise: number;
  holding_period_days: number;
  gain_type: "loss" | "ltcg" | "stcg";
  is_locked: boolean;
  lock_expiry_date: string | null;
  tax_drag_rank: number;
};

export type RecommendedOrder = {
  instrument_id: number | null;
  symbol: string;
  name: string;
  account_id: number | null;
  action: "buy" | "sell";
  amount_paise: number;
  units: string | null;
  current_nav: string | null;
  reason: string;
};

export type RebalancePlanRead = {
  mode: "inflow_only" | "full_rebalance";
  scope_type: string;
  account_id: number | null;
  total_portfolio_value_paise: number;
  inflow_paise: number;
  post_rebalance_portfolio_value_paise: number;
  is_out_of_tolerance: boolean;
  bucket_actions: RebalancingBucketAction[];
  recommended_orders: RecommendedOrder[];
  lot_sell_recommendations: LotSellRecommendation[];
  summary: string;
};

// --------------------------------------------------------------------------- //
// API Calls
// --------------------------------------------------------------------------- //

export function listAllocationTargets(params?: {
  scope?: AllocationScopeType;
  account_id?: number | null;
}): Promise<AllocationTargetRead[]> {
  const qs = new URLSearchParams();
  if (params?.scope) {
    qs.set("scope", params.scope);
  }
  if (params?.account_id != null) {
    qs.set("account_id", String(params.account_id));
  }
  const s = qs.toString();
  return request<AllocationTargetRead[]>(`/rebalancing/targets${s ? `?${s}` : ""}`);
}

export function createAllocationTarget(
  target: AllocationTargetCreate,
): Promise<AllocationTargetRead> {
  return request<AllocationTargetRead>("/rebalancing/targets", {
    method: "POST",
    body: JSON.stringify(target),
  });
}

export function batchSetAllocationTargets(
  batch: AllocationTargetBatchCreate,
): Promise<AllocationTargetRead[]> {
  return request<AllocationTargetRead[]>("/rebalancing/targets/batch", {
    method: "POST",
    body: JSON.stringify(batch),
  });
}

export function getAllocationTarget(targetId: number): Promise<AllocationTargetRead> {
  return request<AllocationTargetRead>(`/rebalancing/targets/${targetId}`);
}

export function updateAllocationTarget(
  targetId: number,
  target: AllocationTargetUpdate,
): Promise<AllocationTargetRead> {
  return request<AllocationTargetRead>(`/rebalancing/targets/${targetId}`, {
    method: "PATCH",
    body: JSON.stringify(target),
  });
}

export function deleteAllocationTarget(targetId: number): Promise<void> {
  return request<void>(`/rebalancing/targets/${targetId}`, {
    method: "DELETE",
  });
}

export function evaluateRebalancing(
  req: RebalanceEvaluateRequest,
): Promise<RebalanceEvaluationRead> {
  return request<RebalanceEvaluationRead>("/rebalancing/evaluate", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export function planRebalancing(
  req: RebalancePlanRequest,
): Promise<RebalancePlanRead> {
  return request<RebalancePlanRead>("/rebalancing/plan", {
    method: "POST",
    body: JSON.stringify(req),
  });
}
