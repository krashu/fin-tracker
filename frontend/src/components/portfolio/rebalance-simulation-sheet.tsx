"use client";

/**
 * Interactive Rebalance Simulation Sheet / Drawer (Milestone 4 Phase 2).
 *
 * Implements:
 * 1. Strategy Selector:
 *    - Mode A (Inflow-Only / SIP Top-Up - Default): Water-filling cash distribution
 *      into underweight buckets with ₹0 tax drag.
 *    - Mode B (Tax-Aware Full Rebalance): Tax-ranked sell recommendations from overweight
 *      buckets, with LTCG/STCG/Loss badges and 3-year ELSS lock-in guardrail.
 * 2. Actionable Order Checklist:
 *    - Consolidated Buy & Sell orders with checkboxes and copyable summary.
 */

import { useEffect, useState, useMemo } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Sensitive } from "@/components/balance-visibility";
import { formatINR, formatPercent, compactINR, rupeesToPaise } from "@/lib/format";
import { MONO } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import {
  useRebalancePlanMutation,
} from "@/src/hooks/use-rebalancing";
import type {
  AllocationScopeType,
  RebalancePlanRead,
  RecommendedOrder,
  LotSellRecommendation,
} from "@/src/api/rebalancing";

export type RebalanceSimulationSheetProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  scope?: AllocationScopeType;
  accountId?: number | null;
  totalPortfolioValuePaise?: number;
};

const INFLOW_PRESETS = [10000, 25000, 50000, 100000];

export function RebalanceSimulationSheet({
  open,
  onOpenChange,
  scope = "all",
  accountId = null,
  totalPortfolioValuePaise = 0,
}: RebalanceSimulationSheetProps) {
  const [mode, setMode] = useState<"inflow_only" | "full_rebalance">("inflow_only");
  const [inflowRupees, setInflowRupees] = useState<string>("25000");
  const [checkedOrders, setCheckedOrders] = useState<Record<string, boolean>>({});
  const [copied, setCopied] = useState<boolean>(false);

  const planMutation = useRebalancePlanMutation();

  const parsedInflowPaise = useMemo(() => {
    return rupeesToPaise(inflowRupees);
  }, [inflowRupees]);

  // Trigger rebalance plan calculation when opened or inputs change
  useEffect(() => {
    if (!open) return;

    planMutation.mutate({
      scope_type: scope,
      account_id: scope === "account" ? accountId : null,
      mode,
      inflow_paise: mode === "inflow_only" ? parsedInflowPaise : 0,
    });
    // Reset checked orders when re-calculating
    setCheckedOrders({});
    setCopied(false);
  }, [open, mode, parsedInflowPaise, scope, accountId]);

  const plan: RebalancePlanRead | undefined = planMutation.data;
  const isLoading = planMutation.isPending;

  const handleToggleOrder = (orderKey: string) => {
    setCheckedOrders((prev) => ({
      ...prev,
      [orderKey]: !prev[orderKey],
    }));
  };

  const handleCopySummary = () => {
    if (!plan) return;

    const lines: string[] = [
      `=== Portfolio Rebalance Plan (${plan.mode === "inflow_only" ? "Mode A: Inflow-Only Water Filling" : "Mode B: Tax-Aware Full Rebalance"}) ===`,
      `Portfolio Value: ₹${(plan.total_portfolio_value_paise / 100).toLocaleString("en-IN")}`,
    ];

    if (plan.mode === "inflow_only") {
      lines.push(`Fresh Inflow Deployed: ₹${(plan.inflow_paise / 100).toLocaleString("en-IN")}`);
    }

    lines.push("");
    lines.push("--- Recommended Actionable Orders ---");

    if (plan.recommended_orders.length === 0) {
      lines.push("No orders needed. Portfolio is already within target bands.");
    } else {
      plan.recommended_orders.forEach((ord, idx) => {
        const checked = checkedOrders[`${ord.action}-${ord.symbol}-${idx}`] ? "[✓]" : "[ ]";
        const amt = `₹${(ord.amount_paise / 100).toLocaleString("en-IN")}`;
        const units = ord.units ? ` (~${ord.units} units)` : "";
        lines.push(`${checked} ${ord.action.toUpperCase()}: ${ord.symbol} (${ord.name}) - ${amt}${units}`);
        lines.push(`   Reason: ${ord.reason}`);
      });
    }

    if (plan.mode === "full_rebalance" && plan.lot_sell_recommendations.length > 0) {
      lines.push("");
      lines.push("--- Tax-Optimized Sell Lots ---");
      plan.lot_sell_recommendations.forEach((lot) => {
        const lockStr = lot.is_locked ? " [LOCKED ELSS - DO NOT SELL]" : "";
        lines.push(
          `• ${lot.symbol}: Sell ${lot.units_to_sell} units (Acquired: ${lot.acquired_on}, Holding: ${lot.holding_period_days}d, Gain: ₹${(lot.estimated_gain_paise / 100).toLocaleString("en-IN")} ${lot.gain_type.toUpperCase()})${lockStr}`
        );
      });
    }

    if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(lines.join("\n"));
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 2500);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-3xl">
        <DialogHeader>
          <div className="flex items-center justify-between pr-4">
            <DialogTitle className="text-lg font-semibold">
              Rebalance Portfolio
            </DialogTitle>
            <Badge
              variant="outline"
              className={cn(
                "text-[10px] font-semibold uppercase tracking-wider",
                mode === "inflow_only"
                  ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                  : "border-amber-500/30 bg-amber-500/10 text-amber-600 dark:text-amber-400",
              )}
            >
              {mode === "inflow_only" ? "Zero Tax Drag" : "Tax-Aware"}
            </Badge>
          </div>
          <DialogDescription className="text-xs text-muted-foreground">
            Simulate optimal asset realignment using cash water-filling or tax-ranked lot liquidations.
          </DialogDescription>
        </DialogHeader>

        {/* 1. Strategy Selector Tabs */}
        <div className="flex flex-col gap-4 border-b pb-4">
          <div className="grid grid-cols-2 gap-2 rounded-lg bg-muted p-1">
            <button
              type="button"
              onClick={() => setMode("inflow_only")}
              className={cn(
                "flex flex-col items-center justify-center rounded-md py-2 px-3 text-xs font-medium transition-colors",
                mode === "inflow_only"
                  ? "bg-card text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              <span className="font-semibold">Mode A: Inflow-Only (Water-Filling)</span>
              <span className="text-[10px] opacity-75">
                Deploy fresh SIP capital • ₹0 tax drag
              </span>
            </button>
            <button
              type="button"
              onClick={() => setMode("full_rebalance")}
              className={cn(
                "flex flex-col items-center justify-center rounded-md py-2 px-3 text-xs font-medium transition-colors",
                mode === "full_rebalance"
                  ? "bg-card text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              <span className="font-semibold">Mode B: Tax-Aware Full Rebalance</span>
              <span className="text-[10px] opacity-75">
                Sell overweight lots • ELSS lock guard
              </span>
            </button>
          </div>

          {/* Mode A Cash Inflow Input */}
          {mode === "inflow_only" && (
            <div className="flex flex-col gap-2 rounded-lg border bg-muted/30 p-3.5">
              <div className="flex items-center justify-between">
                <label
                  htmlFor="inflow-amount-input"
                  className="text-xs font-medium text-foreground"
                >
                  Fresh Monthly Investment Inflow (₹)
                </label>
                <span className="text-[11px] text-muted-foreground">
                  Water-fills lowest underweight buckets first
                </span>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <div className="relative flex-1 min-w-[140px]">
                  <span className="absolute left-3 top-1/2 -translate-y-1/2 text-xs font-medium text-muted-foreground">
                    ₹
                  </span>
                  <input
                    id="inflow-amount-input"
                    type="number"
                    min="0"
                    step="1000"
                    value={inflowRupees}
                    onChange={(e) => setInflowRupees(e.target.value)}
                    className="h-9 w-full rounded-md border border-input bg-card pl-7 pr-3 text-xs font-medium text-foreground shadow-sm focus:border-ring focus:outline-none focus:ring-1 focus:ring-ring"
                    placeholder="25000"
                  />
                </div>
                <div className="flex flex-wrap items-center gap-1.5">
                  {INFLOW_PRESETS.map((preset) => (
                    <Button
                      key={preset}
                      type="button"
                      variant="outline"
                      size="sm"
                      className="h-8 px-2.5 text-xs font-medium"
                      onClick={() => setInflowRupees(String(preset))}
                    >
                      +{compactINR(preset * 100)}
                    </Button>
                  ))}
                </div>
              </div>
            </div>
          )}

          {/* Mode B Statutory Guardrail Notice */}
          {mode === "full_rebalance" && (
            <div className="flex items-start gap-2.5 rounded-lg border border-amber-500/20 bg-amber-500/5 p-3 text-xs text-amber-800 dark:text-amber-300">
              <svg
                className="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-400"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth="2"
                  d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
                />
              </svg>
              <div>
                <p className="font-medium">Statutory Tax & ELSS Guardrails Active</p>
                <p className="mt-0.5 text-[11px] opacity-90 leading-relaxed">
                  Lots are evaluated by lowest marginal tax drag (Loss harvesting → LTCG at 12.5% → STCG at 20%).
                  Locked ELSS units (&lt; 3-year statutory holding period) are strictly preserved and excluded from sales.
                </p>
              </div>
            </div>
          )}
        </div>

        {/* 2. Simulation Results / Loading state */}
        {isLoading && (
          <div className="flex flex-col items-center justify-center py-12 text-center text-xs text-muted-foreground">
            <span className="size-6 animate-spin rounded-full border-2 border-primary border-t-transparent mb-3" />
            Solving rebalancing distribution...
          </div>
        )}

        {!isLoading && plan && (
          <div className="flex flex-col gap-6 py-2">
            {/* Water-Filling Allocation Table */}
            <div className="flex flex-col gap-2">
              <div className="flex items-center justify-between">
                <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  Bucket Allocation & Cashflow Distribution
                </h4>
                <span className="text-[11px] text-muted-foreground">
                  Post-rebalance Portfolio:{" "}
                  <Sensitive>
                    <span className="font-semibold text-foreground" style={MONO}>
                      {formatINR(plan.post_rebalance_portfolio_value_paise)}
                    </span>
                  </Sensitive>
                </span>
              </div>

              <div className="overflow-x-auto rounded-lg border">
                <table className="w-full text-left text-xs">
                  <thead className="border-b bg-muted/60 text-[11px] font-medium text-muted-foreground">
                    <tr>
                      <th className="py-2.5 px-3">Bucket</th>
                      <th className="py-2.5 px-3 text-right">Target %</th>
                      <th className="py-2.5 px-3 text-right">Pre-Rebalance</th>
                      <th className="py-2.5 px-3 text-right">Recommended Cashflow</th>
                      <th className="py-2.5 px-3 text-right">Post-Rebalance</th>
                      <th className="py-2.5 px-3 text-right">Post Drift</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {plan.bucket_actions.map((b) => {
                      const isFunded = b.recommended_cashflow_paise > 0;
                      const isSold = b.recommended_cashflow_paise < 0;

                      return (
                        <tr key={b.target_key} className="hover:bg-muted/30">
                          <td className="py-2 px-3 font-medium text-foreground capitalize">
                            {b.target_key.replace(/_/g, " ")}
                          </td>
                          <td className="py-2 px-3 text-right font-medium text-muted-foreground" style={MONO}>
                            {formatPercent(b.target_bps / 10000)}
                          </td>
                          <td className="py-2 px-3 text-right text-muted-foreground" style={MONO}>
                            <div>{formatPercent(b.pre_rebalance_bps / 10000)}</div>
                            <div className="text-[10px] text-muted-foreground/75">
                              <Sensitive>{formatINR(b.pre_rebalance_value_paise)}</Sensitive>
                            </div>
                          </td>
                          <td
                            className={cn(
                              "py-2 px-3 text-right font-semibold",
                              isFunded
                                ? "text-emerald-600 dark:text-emerald-400"
                                : isSold
                                  ? "text-rose-600 dark:text-rose-400"
                                  : "text-muted-foreground",
                            )}
                            style={MONO}
                          >
                            <Sensitive>
                              {isFunded
                                ? `+${formatINR(b.recommended_cashflow_paise)}`
                                : isSold
                                  ? `−${formatINR(Math.abs(b.recommended_cashflow_paise))}`
                                  : "₹0"}
                            </Sensitive>
                          </td>
                          <td className="py-2 px-3 text-right text-foreground font-medium" style={MONO}>
                            <div>{formatPercent(b.post_rebalance_bps / 10000)}</div>
                            <div className="text-[10px] text-muted-foreground">
                              <Sensitive>{formatINR(b.post_rebalance_value_paise)}</Sensitive>
                            </div>
                          </td>
                          <td
                            className={cn(
                              "py-2 px-3 text-right font-medium",
                              Math.abs(b.post_rebalance_drift_bps) <= 100
                                ? "text-emerald-600 dark:text-emerald-400"
                                : "text-amber-600 dark:text-amber-400",
                            )}
                            style={MONO}
                          >
                            {b.post_rebalance_drift_bps > 0 ? "+" : ""}
                            {(b.post_rebalance_drift_bps / 100).toFixed(1)}%
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>

            {/* Mode B: Recommended Lot Sells & ELSS Lockout Display */}
            {mode === "full_rebalance" && plan.lot_sell_recommendations.length > 0 && (
              <div className="flex flex-col gap-2">
                <div className="flex items-center justify-between">
                  <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    Tax-Ranked Lot Sell Recommendations
                  </h4>
                  <span className="text-[11px] text-muted-foreground">
                    Sorted by lowest marginal tax drag
                  </span>
                </div>

                <div className="overflow-x-auto rounded-lg border">
                  <table className="w-full text-left text-xs">
                    <thead className="border-b bg-muted/60 text-[11px] font-medium text-muted-foreground">
                      <tr>
                        <th className="py-2 px-3">Rank & Instrument</th>
                        <th className="py-2 px-3">Acquisition</th>
                        <th className="py-2 px-3 text-right">Units to Sell</th>
                        <th className="py-2 px-3 text-right">Proceeds</th>
                        <th className="py-2 px-3 text-right">Est. Gain / Loss</th>
                        <th className="py-2 px-3 text-center">Tax Category</th>
                        <th className="py-2 px-3 text-center">Status</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {plan.lot_sell_recommendations.map((lot) => (
                        <tr
                          key={lot.lot_txn_id}
                          className={cn(
                            "hover:bg-muted/30",
                            lot.is_locked && "bg-muted/20 opacity-75",
                          )}
                        >
                          <td className="py-2 px-3 font-medium text-foreground">
                            <div className="flex items-center gap-1.5">
                              <span className="grid size-4 place-items-center rounded bg-muted text-[10px] font-bold text-muted-foreground">
                                #{lot.tax_drag_rank}
                              </span>
                              <span>{lot.symbol}</span>
                            </div>
                            <div className="truncate text-[10px] text-muted-foreground max-w-[180px]">
                              {lot.name}
                            </div>
                          </td>
                          <td className="py-2 px-3 text-muted-foreground">
                            <div>{lot.acquired_on}</div>
                            <div className="text-[10px] text-muted-foreground/75">
                              {lot.holding_period_days} days held
                            </div>
                          </td>
                          <td className="py-2 px-3 text-right font-medium" style={MONO}>
                            {lot.units_to_sell}
                          </td>
                          <td className="py-2 px-3 text-right font-medium" style={MONO}>
                            <Sensitive>{formatINR(lot.estimated_proceeds_paise)}</Sensitive>
                          </td>
                          <td
                            className={cn(
                              "py-2 px-3 text-right font-semibold",
                              lot.estimated_gain_paise < 0
                                ? "text-emerald-600 dark:text-emerald-400"
                                : lot.gain_type === "ltcg"
                                  ? "text-blue-600 dark:text-blue-400"
                                  : "text-amber-600 dark:text-amber-400",
                            )}
                            style={MONO}
                          >
                            <Sensitive>
                              {lot.estimated_gain_paise > 0 ? "+" : ""}
                              {formatINR(lot.estimated_gain_paise)}
                            </Sensitive>
                          </td>
                          <td className="py-2 px-3 text-center">
                            <Badge
                              variant="outline"
                              className={cn(
                                "text-[10px] font-semibold uppercase",
                                lot.gain_type === "loss" &&
                                  "border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
                                lot.gain_type === "ltcg" &&
                                  "border-blue-500/30 bg-blue-500/10 text-blue-600 dark:text-blue-400",
                                lot.gain_type === "stcg" &&
                                  "border-amber-500/30 bg-amber-500/10 text-amber-600 dark:text-amber-400",
                              )}
                            >
                              {lot.gain_type === "loss"
                                ? "Loss Harvest"
                                : lot.gain_type === "ltcg"
                                  ? "LTCG (12.5%)"
                                  : "STCG (20%)"}
                            </Badge>
                          </td>
                          <td className="py-2 px-3 text-center">
                            {lot.is_locked ? (
                              <TooltipProvider>
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <span className="inline-flex items-center gap-1 text-[11px] font-medium text-rose-600 dark:text-rose-400 cursor-help">
                                      <svg
                                        className="size-3.5"
                                        fill="none"
                                        viewBox="0 0 24 24"
                                        stroke="currentColor"
                                      >
                                        <path
                                          strokeLinecap="round"
                                          strokeLinejoin="round"
                                          strokeWidth="2"
                                          d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"
                                        />
                                      </svg>
                                      Locked
                                    </span>
                                  </TooltipTrigger>
                                  <TooltipContent>
                                    Statutory 3-year ELSS lock-in expires {lot.lock_expiry_date ?? "soon"}. Units cannot be sold.
                                  </TooltipContent>
                                </Tooltip>
                              </TooltipProvider>
                            ) : (
                              <span className="text-[11px] font-medium text-emerald-600 dark:text-emerald-400">
                                Unlocked
                              </span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {/* 3. Actionable Order Checklist */}
            <div className="flex flex-col gap-2">
              <div className="flex items-center justify-between">
                <div>
                  <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    Actionable Order Checklist
                  </h4>
                  <p className="text-[11px] text-muted-foreground">
                    Execute these trades with your broker to rebalance your portfolio.
                  </p>
                </div>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={handleCopySummary}
                  className="h-8 gap-1.5 text-xs font-medium"
                >
                  {copied ? (
                    <>
                      <svg className="size-3.5 text-emerald-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M5 13l4 4L19 7" />
                      </svg>
                      Copied!
                    </>
                  ) : (
                    <>
                      <svg className="size-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                      </svg>
                      Copy Order List
                    </>
                  )}
                </Button>
              </div>

              {plan.recommended_orders.length === 0 ? (
                <div className="rounded-lg border border-dashed p-6 text-center text-xs text-muted-foreground">
                  No orders required! Your portfolio is already well within target allocation tolerances.
                </div>
              ) : (
                <div className="flex flex-col gap-2">
                  {plan.recommended_orders.map((ord, idx) => {
                    const orderKey = `${ord.action}-${ord.symbol}-${idx}`;
                    const isChecked = !!checkedOrders[orderKey];

                    return (
                      <div
                        key={orderKey}
                        onClick={() => handleToggleOrder(orderKey)}
                        className={cn(
                          "flex cursor-pointer items-center justify-between rounded-lg border p-3 transition-colors",
                          isChecked
                            ? "border-muted bg-muted/40 opacity-60"
                            : "hover:bg-muted/20 border-border bg-card",
                        )}
                      >
                        <div className="flex items-center gap-3">
                          <input
                            type="checkbox"
                            checked={isChecked}
                            onChange={() => handleToggleOrder(orderKey)}
                            onClick={(e) => e.stopPropagation()}
                            className="size-4 rounded border-input text-primary focus:ring-ring"
                          />
                          <div>
                            <div className="flex items-center gap-2">
                              <Badge
                                variant="outline"
                                className={cn(
                                  "text-[10px] font-bold uppercase",
                                  ord.action === "buy"
                                    ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                                    : "border-rose-500/30 bg-rose-500/10 text-rose-600 dark:text-rose-400",
                                )}
                              >
                                {ord.action.toUpperCase()}
                              </Badge>
                              <span
                                className={cn(
                                  "text-xs font-semibold text-foreground",
                                  isChecked && "line-through text-muted-foreground",
                                )}
                              >
                                {ord.symbol}
                              </span>
                              <span className="text-[11px] text-muted-foreground">
                                ({ord.name})
                              </span>
                            </div>
                            <p className="mt-0.5 text-[11px] text-muted-foreground">
                              {ord.reason}
                            </p>
                          </div>
                        </div>

                        <div className="text-right">
                          <div
                            className={cn(
                              "text-xs font-semibold",
                              ord.action === "buy" ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400",
                              isChecked && "line-through text-muted-foreground",
                            )}
                            style={MONO}
                          >
                            <Sensitive>{formatINR(ord.amount_paise)}</Sensitive>
                          </div>
                          {ord.units && (
                            <div className="text-[10px] text-muted-foreground">
                              ~{ord.units} units
                            </div>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
