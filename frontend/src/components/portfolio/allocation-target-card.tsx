"use client";

/**
 * Target Allocation & Drift Card (Milestone 4 Phase 2).
 *
 * Implements:
 * 1. Grouped Comparison Visualizer / Chart:
 *    - Actual % vs Target % comparison for each allocation bucket.
 *    - 5/25 Swedroe tolerance whiskers (absolute 5% or relative 25% deviation).
 *    - Out-of-tolerance status badges and holding counts.
 * 2. Target Allocation Config Sheet / Modal:
 *    - Quick preset templates ("60/40 Equity-Debt", "Aggressive 70/20/10", "Classic 3-Fund").
 *    - Real-time Sum = 100.00% validation guard before saving.
 * 3. Rebalance Simulation Sheet Trigger:
 *    - Launches interactive water-filling (Mode A) and tax-aware (Mode B) simulator.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Sensitive } from "@/components/balance-visibility";
import { formatINR, formatPercent, compactINR } from "@/lib/format";
import { MONO } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { getInvestmentAccounts } from "@/lib/api/investment-accounts";
import {
  useAllocationTargets,
  useRebalanceEvaluation,
  useSaveAllocationTargetsMutation,
} from "@/src/hooks/use-rebalancing";
import type {
  AllocationScopeType,
  AllocationTargetCreate,
  BucketDriftRead,
} from "@/src/api/rebalancing";
import { RebalanceSimulationSheet } from "./rebalance-simulation-sheet";

export type AllocationTargetCardProps = {
  initialScope?: AllocationScopeType;
  initialAccountId?: number | null;
  scope?: AllocationScopeType;
  accountId?: number | null;
  onScopeChange?: (scope: AllocationScopeType, accountId?: number | null) => void;
  hideScopeSelector?: boolean;
  className?: string;
};

// Quick-start preset templates
const PRESETS: Record<string, { label: string; description: string; targets: AllocationTargetCreate[] }> = {
  "60_40": {
    label: "60/40 Equity-Debt",
    description: "Balanced classic portfolio: 60% Equity, 40% Fixed Income",
    targets: [
      { target_type: "asset_class", target_key: "equity", target_bps: 6000, rebalance_threshold_bps: 500 },
      { target_type: "asset_class", target_key: "debt", target_bps: 4000, rebalance_threshold_bps: 500 },
    ],
  },
  "70_20_10": {
    label: "Aggressive 70/20/10",
    description: "Growth tilt with gold hedge: 70% Equity, 20% Debt, 10% Gold",
    targets: [
      { target_type: "asset_class", target_key: "equity", target_bps: 7000, rebalance_threshold_bps: 500 },
      { target_type: "asset_class", target_key: "debt", target_bps: 2000, rebalance_threshold_bps: 500 },
      { target_type: "asset_class", target_key: "gold", target_bps: 1000, rebalance_threshold_bps: 500 },
    ],
  },
  "three_fund": {
    label: "Classic 3-Fund",
    description: "Global diversification: 50% Indian Equity, 20% US Equity, 30% Debt",
    targets: [
      { target_type: "asset_class", target_key: "indian_equity", target_bps: 5000, rebalance_threshold_bps: 500 },
      { target_type: "asset_class", target_key: "us_equity", target_bps: 2000, rebalance_threshold_bps: 500 },
      { target_type: "asset_class", target_key: "debt", target_bps: 3000, rebalance_threshold_bps: 500 },
    ],
  },
};

const COMMON_ASSET_CLASSES = [
  { value: "equity", label: "Equity (All)" },
  { value: "indian_equity", label: "Indian Equity" },
  { value: "indian_mf", label: "Indian Mutual Funds" },
  { value: "us_equity", label: "US Equity & ETFs" },
  { value: "debt", label: "Debt & Fixed Income" },
  { value: "bond", label: "Bonds" },
  { value: "fd", label: "Fixed Deposits" },
  { value: "gold", label: "Gold" },
  { value: "nps", label: "NPS" },
  { value: "other", label: "Other" },
];

export function AllocationTargetCard({
  initialScope = "all",
  initialAccountId = null,
  scope: controlledScope,
  accountId: controlledAccountId,
  onScopeChange,
  hideScopeSelector = false,
  className,
}: AllocationTargetCardProps) {
  const [internalScope, setInternalScope] = useState<AllocationScopeType>(initialScope);
  const [internalAccountId, setInternalAccountId] = useState<number | null>(initialAccountId);

  const scope = controlledScope !== undefined ? controlledScope : internalScope;
  const accountId = controlledAccountId !== undefined ? controlledAccountId : internalAccountId;

  const handleScopeSelect = (newScope: AllocationScopeType, newAccountId: number | null) => {
    if (controlledScope === undefined) {
      setInternalScope(newScope);
      setInternalAccountId(newAccountId);
    }
    onScopeChange?.(newScope, newAccountId);
  };
  const [isConfigOpen, setIsConfigOpen] = useState(false);
  const [isRebalanceOpen, setIsRebalanceOpen] = useState(false);

  // 1. Fetch investment accounts for scope dropdown
  const accountsQuery = useQuery({
    queryKey: ["investment-accounts", { is_active: true }],
    queryFn: () => getInvestmentAccounts({ is_active: true }),
  });
  const accounts = accountsQuery.data ?? [];

  // 2. Fetch targets & drift evaluation
  const targetsQuery = useAllocationTargets(scope, accountId);
  const evaluationQuery = useRebalanceEvaluation(scope, accountId);

  const targets = targetsQuery.data ?? [];
  const evaluation = evaluationQuery.data;
  const isPending = targetsQuery.isPending || evaluationQuery.isPending;
  const hasTargets = targets.length > 0;

  return (
    <>
      <Card className={cn("overflow-hidden border-border bg-card", className)}>
        <CardHeader className="flex flex-col gap-4 border-b bg-muted/20 pb-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <div className="flex items-center gap-2">
              <CardTitle className="text-base font-semibold tracking-tight text-foreground">
                Target Allocation & Drift
              </CardTitle>
              {evaluation && hasTargets && (
                <Badge
                  variant="outline"
                  className={cn(
                    "text-[10px] font-semibold uppercase tracking-wider",
                    evaluation.is_out_of_tolerance
                      ? "border-amber-500/30 bg-amber-500/10 text-amber-600 dark:text-amber-400"
                      : "border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
                  )}
                >
                  {evaluation.is_out_of_tolerance
                    ? "Out of Tolerance"
                    : "Within 5/25 Tolerance"}
                </Badge>
              )}
            </div>
            <p className="mt-0.5 text-xs text-muted-foreground">
              Monitor asset weights against target bands and Swedroe 5/25 tolerance heuristic.
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {/* Scope Filter */}
            {!hideScopeSelector && accounts.length > 0 && (
              <select
                aria-label="Allocation Scope"
                value={scope === "account" ? `account:${accountId}` : "all"}
                onChange={(e) => {
                  const val = e.target.value;
                  if (val === "all") {
                    handleScopeSelect("all", null);
                  } else {
                    const accId = Number(val.replace("account:", ""));
                    handleScopeSelect("account", accId);
                  }
                }}
                className="h-8 rounded-md border border-input bg-card px-2.5 text-xs font-medium text-foreground shadow-sm focus:border-ring focus:outline-none"
              >
                <option value="all">Portfolio-Wide (All Accounts)</option>
                {accounts.map((acc) => (
                  <option key={acc.id} value={`account:${acc.id}`}>
                    {acc.name}
                  </option>
                ))}
              </select>
            )}

            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setIsConfigOpen(true)}
              className="h-8 text-xs font-medium"
            >
              {hasTargets ? "Edit Targets" : "Set Target Allocation"}
            </Button>

            {hasTargets && (
              <Button
                type="button"
                size="sm"
                onClick={() => setIsRebalanceOpen(true)}
                className="h-8 gap-1.5 text-xs font-semibold shadow-sm"
              >
                <svg className="size-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
                Rebalance Portfolio
              </Button>
            )}
          </div>
        </CardHeader>

        <CardContent className="p-5">
          {/* Loading state */}
          {isPending && (
            <div className="flex items-center justify-center py-12 text-xs text-muted-foreground">
              <span className="size-4 animate-spin rounded-full border-2 border-primary border-t-transparent mr-2" />
              Evaluating portfolio drift...
            </div>
          )}

          {/* Empty State when no targets defined */}
          {!isPending && !hasTargets && (
            <div className="flex flex-col items-center justify-center py-10 text-center">
              <div className="grid size-12 place-items-center rounded-full bg-muted/50 mb-3 text-muted-foreground">
                <svg className="size-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" d="M11 3.055A9.001 9.001 0 1020.945 13H11V3.055z" />
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" d="M20.488 9H15V3.512A9.025 9.025 0 0120.488 9z" />
                </svg>
              </div>
              <h3 className="text-sm font-semibold text-foreground">
                No Target Allocation Configured
              </h3>
              <p className="mt-1 max-w-md text-xs text-muted-foreground leading-relaxed">
                Define your ideal asset allocation (e.g., 60/40 Equity-Debt or 3-Fund) to automatically
                track drift and simulate tax-optimized rebalancing.
              </p>
              <Button
                type="button"
                onClick={() => setIsConfigOpen(true)}
                className="mt-4 h-8 text-xs font-semibold"
              >
                Set Up Allocation Targets
              </Button>
            </div>
          )}

          {/* Evaluation Visualizer */}
          {!isPending && hasTargets && evaluation && (
            <div className="flex flex-col gap-6">
              {/* Summary Stats Row */}
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <div className="rounded-lg border bg-muted/20 p-3">
                  <div className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">
                    Total Portfolio
                  </div>
                  <div className="mt-1 text-sm font-bold text-foreground" style={MONO}>
                    <Sensitive>{formatINR(evaluation.total_portfolio_value_paise)}</Sensitive>
                  </div>
                </div>

                <div className="rounded-lg border bg-muted/20 p-3">
                  <div className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">
                    Target Sum
                  </div>
                  <div className="mt-1 text-sm font-bold text-foreground" style={MONO}>
                    {formatPercent(evaluation.total_target_bps / 10000)}
                  </div>
                </div>

                <div className="rounded-lg border bg-muted/20 p-3">
                  <div className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">
                    Tolerance Status
                  </div>
                  <div className="mt-1 flex items-center gap-1.5 text-xs font-semibold">
                    <span
                      className={cn(
                        "size-2 rounded-full",
                        evaluation.is_out_of_tolerance ? "bg-amber-500" : "bg-emerald-500",
                      )}
                    />
                    <span
                      className={
                        evaluation.is_out_of_tolerance ? "text-amber-600 dark:text-amber-400" : "text-emerald-600 dark:text-emerald-400"
                      }
                    >
                      {evaluation.is_out_of_tolerance ? "Action Needed" : "Balanced"}
                    </span>
                  </div>
                </div>

                <div className="rounded-lg border bg-muted/20 p-3">
                  <div className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">
                    Unallocated
                  </div>
                  <div className="mt-1 text-sm font-bold text-muted-foreground" style={MONO}>
                    {evaluation.unallocated_bps > 0 ? (
                      <Sensitive>
                        {formatINR(evaluation.unallocated_value_paise)} ({formatPercent(evaluation.unallocated_bps / 10000)})
                      </Sensitive>
                    ) : (
                      "₹0 (0.0%)"
                    )}
                  </div>
                </div>
              </div>

              {/* Grouped Comparison Chart with Tolerance Whiskers */}
              <div className="flex flex-col gap-4">
                <div className="flex items-center justify-between text-xs text-muted-foreground font-medium">
                  <span>Allocation Buckets</span>
                  <span>Swedroe 5/25 Tolerance Bands</span>
                </div>

                <div className="flex flex-col gap-4">
                  {evaluation.buckets.map((b) => (
                    <BucketComparisonRow key={b.target_key} bucket={b} />
                  ))}
                </div>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Target Allocation Config Modal */}
      <TargetAllocationConfigModal
        open={isConfigOpen}
        onOpenChange={setIsConfigOpen}
        scope={scope}
        accountId={accountId}
        existingTargets={targets}
      />

      {/* Rebalance Simulation Sheet */}
      <RebalanceSimulationSheet
        open={isRebalanceOpen}
        onOpenChange={setIsRebalanceOpen}
        scope={scope}
        accountId={accountId}
        totalPortfolioValuePaise={evaluation?.total_portfolio_value_paise ?? 0}
      />
    </>
  );
}

/**
 * Single bucket comparison row with grouped visualizer & tolerance whiskers.
 */
function BucketComparisonRow({ bucket }: { bucket: BucketDriftRead }) {
  const targetPct = bucket.target_bps / 100;
  const actualPct = bucket.actual_bps / 100;
  const thresholdPct = bucket.rebalance_threshold_bps / 100;

  // Swedroe tolerance boundaries [minBand, maxBand]
  const minTolerance = Math.max(0, targetPct - thresholdPct);
  const maxTolerance = Math.min(100, targetPct + thresholdPct);

  // Scaled max width for visual bar representation (at least 100% or slightly above highest bucket)
  const maxScale = Math.max(100, Math.ceil(Math.max(targetPct + thresholdPct, actualPct) / 10) * 10);

  const targetLeftPercent = (targetPct / maxScale) * 100;
  const actualWidthPercent = (actualPct / maxScale) * 100;
  const whiskerLeftPercent = (minTolerance / maxScale) * 100;
  const whiskerWidthPercent = ((maxTolerance - minTolerance) / maxScale) * 100;

  return (
    <div className="flex flex-col gap-2 rounded-lg border bg-muted/10 p-3.5 transition-colors hover:bg-muted/20">
      {/* Bucket Header Info */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-xs font-bold text-foreground capitalize">
            {bucket.target_key.replace(/_/g, " ")}
          </span>
          <Badge variant="outline" className="text-[10px] text-muted-foreground uppercase">
            {bucket.target_type.replace(/_/g, " ")}
          </Badge>
          <span className="text-[11px] text-muted-foreground">
            ({bucket.holding_count} {bucket.holding_count === 1 ? "holding" : "holdings"})
          </span>
        </div>

        <div className="flex items-center gap-2 text-xs">
          <span className="text-muted-foreground font-medium">
            Actual: <span className="font-semibold text-foreground" style={MONO}>{actualPct.toFixed(1)}%</span>
            {" "}(<Sensitive>{compactINR(bucket.actual_value_paise)}</Sensitive>)
          </span>
          <span className="text-muted-foreground/60">/</span>
          <span className="text-muted-foreground font-medium">
            Target: <span className="font-semibold text-foreground" style={MONO}>{targetPct.toFixed(1)}%</span>
          </span>

          <Badge
            variant="outline"
            className={cn(
              "text-[10px] font-semibold",
              bucket.is_out_of_tolerance
                ? bucket.drift_bps > 0
                  ? "border-amber-500/30 bg-amber-500/10 text-amber-600 dark:text-amber-400"
                  : "border-rose-500/30 bg-rose-500/10 text-rose-600 dark:text-rose-400"
                : "border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
            )}
          >
            {bucket.drift_bps > 0 ? "+" : ""}
            {(bucket.drift_bps / 100).toFixed(1)}% drift
            {bucket.is_out_of_tolerance ? (bucket.drift_bps > 0 ? " (Overweight)" : " (Underweight)") : " (Balanced)"}
          </Badge>
        </div>
      </div>

      {/* Grouped Horizontal Visualizer with Whisker Band */}
      <div className="relative mt-1 h-5 w-full rounded bg-muted/40">
        {/* Shaded Tolerance Whisker Range [target - threshold, target + threshold] */}
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger asChild>
              <div
                className="absolute top-0 bottom-0 rounded border border-dashed border-primary/40 bg-primary/10"
                style={{
                  left: `${whiskerLeftPercent}%`,
                  width: `${whiskerWidthPercent}%`,
                }}
              />
            </TooltipTrigger>
            <TooltipContent>
              Swedroe 5/25 Tolerance Band: {minTolerance.toFixed(1)}% – {maxTolerance.toFixed(1)}%
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>

        {/* Actual Allocation Bar */}
        <div
          className={cn(
            "absolute top-1 bottom-1 rounded transition-all duration-300",
            bucket.is_out_of_tolerance
              ? bucket.drift_bps > 0
                ? "bg-amber-500"
                : "bg-rose-500"
              : "bg-emerald-500",
          )}
          style={{
            left: 0,
            width: `${Math.min(100, actualWidthPercent)}%`,
          }}
        />

        {/* Target Marker Pin (Vertical Line) */}
        <div
          className="absolute top-0 bottom-0 z-10 w-0.5 bg-foreground"
          style={{
            left: `${targetLeftPercent}%`,
          }}
          title={`Target: ${targetPct.toFixed(1)}%`}
        />
      </div>

      {/* Whisker Ticks & Action Guide */}
      <div className="flex items-center justify-between text-[10px] text-muted-foreground">
        <span>Min Band: {minTolerance.toFixed(1)}%</span>
        <span className="font-medium text-foreground">Target: {targetPct.toFixed(1)}%</span>
        <span>Max Band: {maxTolerance.toFixed(1)}%</span>
      </div>
    </div>
  );
}

/**
 * Modal to configure target allocation weights with presets and sum = 100% guard.
 */
function TargetAllocationConfigModal({
  open,
  onOpenChange,
  scope,
  accountId,
  existingTargets,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  scope: AllocationScopeType;
  accountId: number | null;
  existingTargets: AllocationTargetCreate[];
}) {
  const [targets, setTargets] = useState<AllocationTargetCreate[]>(() => {
    if (existingTargets.length > 0) {
      return existingTargets.map((t) => ({
        target_type: t.target_type ?? "asset_class",
        target_key: t.target_key,
        target_bps: t.target_bps,
        rebalance_threshold_bps: t.rebalance_threshold_bps ?? 500,
      }));
    }
    return PRESETS["60_40"].targets;
  });

  const saveMutation = useSaveAllocationTargetsMutation();

  const totalBps = targets.reduce((sum, t) => sum + (t.target_bps || 0), 0);
  const totalPercent = totalBps / 100;
  const isValidTotal = totalBps === 10000;

  const handleApplyPreset = (presetKey: string) => {
    const preset = PRESETS[presetKey];
    if (!preset) return;
    setTargets(
      preset.targets.map((t) => ({
        ...t,
        scope_type: scope,
        account_id: scope === "account" ? accountId : null,
      })),
    );
  };

  const handleAddTarget = () => {
    setTargets((prev) => [
      ...prev,
      {
        target_type: "asset_class",
        target_key: "other",
        target_bps: Math.max(0, 10000 - totalBps),
        rebalance_threshold_bps: 500,
      },
    ]);
  };

  const handleRemoveTarget = (index: number) => {
    setTargets((prev) => prev.filter((_, idx) => idx !== index));
  };

  const handleUpdateTarget = (index: number, update: Partial<AllocationTargetCreate>) => {
    setTargets((prev) =>
      prev.map((t, idx) => (idx === index ? { ...t, ...update } : t)),
    );
  };

  const handleSave = () => {
    if (!isValidTotal) return;

    saveMutation.mutate(
      {
        scope_type: scope,
        account_id: scope === "account" ? accountId : null,
        targets: targets.map((t) => ({
          ...t,
          scope_type: scope,
          account_id: scope === "account" ? accountId : null,
        })),
      },
      {
        onSuccess: () => {
          onOpenChange(false);
        },
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle className="text-base font-semibold">
            Configure Target Allocation
          </DialogTitle>
          <DialogDescription className="text-xs text-muted-foreground">
            Set target weights for each asset bucket. The sum must equal exactly 100.00%.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4 py-2">
          {/* Preset Buttons */}
          <div className="flex flex-col gap-1.5">
            <span className="text-[11px] font-medium text-muted-foreground">
              Quick Setup Presets
            </span>
            <div className="grid grid-cols-3 gap-2">
              {Object.entries(PRESETS).map(([key, p]) => (
                <button
                  key={key}
                  type="button"
                  onClick={() => handleApplyPreset(key)}
                  className="flex flex-col rounded-md border p-2 text-left hover:border-primary/50 hover:bg-muted/30 transition-colors"
                >
                  <span className="text-xs font-semibold text-foreground">{p.label}</span>
                  <span className="text-[10px] text-muted-foreground line-clamp-1">{p.description}</span>
                </button>
              ))}
            </div>
          </div>

          {/* Allocation Rows */}
          <div className="flex flex-col gap-3">
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">
                Target Allocation Buckets
              </span>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={handleAddTarget}
                className="h-7 text-xs font-medium"
              >
                + Add Bucket
              </Button>
            </div>

            <div className="flex flex-col gap-2">
              {targets.map((t, idx) => (
                <div
                  key={idx}
                  className="flex items-center gap-2 rounded-lg border bg-muted/20 p-2.5"
                >
                  {/* Target Key Selector */}
                  <div className="flex-1 min-w-[120px]">
                    <select
                      aria-label="Target Bucket"
                      value={t.target_key}
                      onChange={(e) => handleUpdateTarget(idx, { target_key: e.target.value })}
                      className="h-8 w-full rounded border border-input bg-card px-2 text-xs font-medium text-foreground focus:border-ring focus:outline-none"
                    >
                      {COMMON_ASSET_CLASSES.map((c) => (
                        <option key={c.value} value={c.value}>
                          {c.label}
                        </option>
                      ))}
                    </select>
                  </div>

                  {/* Target Weight % Input */}
                  <div className="flex items-center gap-1">
                    <input
                      aria-label="Target Weight Percentage"
                      type="number"
                      min="0"
                      max="100"
                      step="0.5"
                      value={(t.target_bps / 100).toFixed(1)}
                      onChange={(e) => {
                        const val = parseFloat(e.target.value) || 0;
                        handleUpdateTarget(idx, { target_bps: Math.round(val * 100) });
                      }}
                      className="h-8 w-16 rounded border border-input bg-card px-1.5 text-right text-xs font-semibold text-foreground focus:border-ring focus:outline-none"
                    />
                    <span className="text-xs font-medium text-muted-foreground">%</span>
                  </div>

                  {/* Tolerance Threshold Input */}
                  <div className="flex items-center gap-1" title="Tolerance threshold (default 5.0%)">
                    <span className="text-[10px] text-muted-foreground">±</span>
                    <input
                      aria-label="Tolerance Band Percentage"
                      type="number"
                      min="0.5"
                      max="20"
                      step="0.5"
                      value={((t.rebalance_threshold_bps ?? 500) / 100).toFixed(1)}
                      onChange={(e) => {
                        const val = parseFloat(e.target.value) || 5;
                        handleUpdateTarget(idx, { rebalance_threshold_bps: Math.round(val * 100) });
                      }}
                      className="h-8 w-14 rounded border border-input bg-card px-1 text-right text-xs font-medium text-muted-foreground focus:border-ring focus:outline-none"
                    />
                    <span className="text-xs font-medium text-muted-foreground">%</span>
                  </div>

                  {/* Remove Button */}
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => handleRemoveTarget(idx)}
                    disabled={targets.length <= 1}
                    className="h-8 w-8 p-0 text-muted-foreground hover:text-rose-600"
                    aria-label="Remove bucket"
                  >
                    <svg className="size-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                    </svg>
                  </Button>
                </div>
              ))}
            </div>
          </div>

          {/* Sum Meter Guard */}
          <div
            className={cn(
              "flex items-center justify-between rounded-lg border p-3 text-xs font-medium",
              isValidTotal
                ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
                : "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400",
            )}
          >
            <div className="flex items-center gap-2">
              <span className="size-2 rounded-full bg-current" />
              <span>
                {isValidTotal
                  ? "Allocation weights sum to exactly 100.00%"
                  : `Allocation sum is ${totalPercent.toFixed(1)}% (must equal 100.00% to save)`}
              </span>
            </div>
            <span className="font-bold" style={MONO}>
              {totalPercent.toFixed(1)}% / 100.00%
            </span>
          </div>
        </div>

        <DialogFooter className="gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => onOpenChange(false)}
          >
            Cancel
          </Button>
          <Button
            type="button"
            size="sm"
            disabled={!isValidTotal || saveMutation.isPending}
            onClick={handleSave}
            className="font-semibold"
          >
            {saveMutation.isPending ? "Saving..." : "Save Target Allocation"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
