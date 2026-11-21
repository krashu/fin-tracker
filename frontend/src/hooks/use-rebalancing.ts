/**
 * TanStack Query hooks for Target Allocation & Intelligent Rebalancing (Milestone 4).
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  batchSetAllocationTargets,
  createAllocationTarget,
  deleteAllocationTarget,
  evaluateRebalancing,
  listAllocationTargets,
  planRebalancing,
  updateAllocationTarget,
  type AllocationScopeType,
  type AllocationTargetBatchCreate,
  type AllocationTargetCreate,
  type AllocationTargetUpdate,
  type RebalancePlanRequest,
} from "@/src/api/rebalancing";

export const rebalancingQueryKeys = {
  all: ["rebalancing"] as const,
  targets: (scope: AllocationScopeType = "all", accountId?: number | null) =>
    ["rebalancing", "targets", scope, accountId ?? null] as const,
  evaluation: (scope: AllocationScopeType = "all", accountId?: number | null) =>
    ["rebalancing", "evaluate", scope, accountId ?? null] as const,
};

/**
 * Fetch allocation targets for given scope and optional account.
 */
export function useAllocationTargets(
  scope: AllocationScopeType = "all",
  accountId?: number | null,
  options?: { enabled?: boolean },
) {
  return useQuery({
    queryKey: rebalancingQueryKeys.targets(scope, accountId),
    queryFn: () => listAllocationTargets({ scope, account_id: accountId }),
    enabled: options?.enabled ?? true,
  });
}

/**
 * Fetch drift evaluation & tolerance status for given scope.
 */
export function useRebalanceEvaluation(
  scope: AllocationScopeType = "all",
  accountId?: number | null,
  options?: { enabled?: boolean },
) {
  return useQuery({
    queryKey: rebalancingQueryKeys.evaluation(scope, accountId),
    queryFn: () =>
      evaluateRebalancing({
        scope_type: scope,
        account_id: accountId ?? null,
      }),
    enabled: options?.enabled ?? true,
  });
}

/**
 * Mutation to atomically batch set/replace targets in a scope.
 */
export function useSaveAllocationTargetsMutation() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (batch: AllocationTargetBatchCreate) =>
      batchSetAllocationTargets(batch),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: rebalancingQueryKeys.all });
    },
  });
}

/**
 * Mutation to create a single allocation target.
 */
export function useCreateAllocationTargetMutation() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (target: AllocationTargetCreate) =>
      createAllocationTarget(target),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: rebalancingQueryKeys.all });
    },
  });
}

/**
 * Mutation to update an allocation target.
 */
export function useUpdateAllocationTargetMutation() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      id,
      update,
    }: {
      id: number;
      update: AllocationTargetUpdate;
    }) => updateAllocationTarget(id, update),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: rebalancingQueryKeys.all });
    },
  });
}

/**
 * Mutation to delete an allocation target.
 */
export function useDeleteAllocationTargetMutation() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (targetId: number) => deleteAllocationTarget(targetId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: rebalancingQueryKeys.all });
    },
  });
}

/**
 * Mutation to compute a rebalance optimization plan (Mode A or Mode B).
 */
export function useRebalancePlanMutation() {
  return useMutation({
    mutationFn: (req: RebalancePlanRequest) => planRebalancing(req),
  });
}
