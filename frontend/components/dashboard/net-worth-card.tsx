"use client";

/**
 * Net Worth Timeline & Wealth Trajectory Card (PRD §F8 View 4 & Wealth Analytics).
 *
 * Features:
 * - Headline scalar net worth with 30-day and 1-year deltas (₹ and %)
 * - Asset vs Liability overview
 * - Toggleable Composition:
 *   - "total" mode: Single smooth area chart displaying aggregate net worth curve
 *   - "breakdown" mode: Stacked Area Chart across 7 asset classes + overlay line
 * - Distinct #f43f5e dashed line overlay (strokeDasharray="4 3") for liabilities (PRD §F8 View 4: no credit cards)
 * - Milestone guides: ₹10L, ₹25L, ₹50L, ₹1Cr
 * - Range filter pills: 1M | 6M | 1Y | 3Y | 5Y | ALL
 * - Privacy masking via Sensitive and useBalanceHidden()
 * - Respects embedded mode: suppresses redundant header chrome when hosted in MacroWealthDeck
 */

import { useMemo, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  XAxis,
  YAxis,
} from "recharts";

import { Sensitive, useBalanceHidden } from "@/components/balance-visibility";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import {
  ChartContainer,
  ChartTooltip,
  type ChartConfig,
} from "@/components/ui/chart";
import { Eyebrow } from "@/components/ui/eyebrow";
import {
  getNetWorthSummary,
  getNetWorthTimeline,
  type OverviewResponse,
  type TimelineRange,
} from "@/lib/api/client";
import { compactINR, formatINR } from "@/lib/format";
import { cn } from "@/lib/utils";

const CHART_CONFIG = {
  net_worth: { label: "Net Worth", color: "#10b981" },
  bank_cash: { label: "Cash & Bank", color: "#0ea5e9" },
  indian_mf: { label: "Mutual Funds", color: "#8b5cf6" },
  indian_equity: { label: "Equity", color: "#3b82f6" },
  us_equity: { label: "US Stocks", color: "#ec4899" },
  debt_fd: { label: "Fixed Income", color: "#64748b" },
  gold: { label: "Gold", color: "#eab308" },
  peer_receivables: { label: "Peer Receivables", color: "#14b8a6" },
  liabilities: { label: "Liabilities", color: "#f43f5e" },
} satisfies ChartConfig;

const RANGES: TimelineRange[] = ["1M", "6M", "1Y", "3Y", "5Y", "ALL"];

const MILESTONES = [
  { value: 1_000_000, label: "₹10L" },
  { value: 2_500_000, label: "₹25L" },
  { value: 5_000_000, label: "₹50L" },
  { value: 10_000_000, label: "₹1Cr" },
];

function getNiceTicks(maxVal: number, targetCount = 4): { ticks: number[]; yMax: number } {
  if (maxVal <= 0) return { ticks: [0], yMax: 1000 };
  const rawStep = maxVal / targetCount;
  const mag = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const fraction = rawStep / mag;
  let stepMultiplier = 1;
  if (fraction > 5) stepMultiplier = 10;
  else if (fraction > 2) stepMultiplier = 5;
  else if (fraction > 1) stepMultiplier = 2;
  const step = stepMultiplier * mag;
  const yMax = Math.ceil(maxVal / step) * step;
  const ticks: number[] = [];
  for (let t = 0; t <= yMax; t += step) {
    ticks.push(t);
  }
  return { ticks, yMax };
}

function ArrowUpRightIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      {...props}
    >
      <path d="M4.5 11.5L11.5 4.5M11.5 4.5H5.5M11.5 4.5V10.5" />
    </svg>
  );
}

function TrendingUpIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      {...props}
    >
      <path d="M1.5 11.5L6.5 6.5L9.5 9.5L14.5 4.5M14.5 4.5H10.5M14.5 4.5V8.5" />
    </svg>
  );
}

function TrendingDownIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      {...props}
    >
      <path d="M1.5 4.5L6.5 9.5L9.5 6.5L14.5 11.5M14.5 11.5H10.5M14.5 11.5V7.5" />
    </svg>
  );
}

export interface NetWorthCardProps {
  className?: string;
  overview?: OverviewResponse;
  embedded?: boolean;
  composition?: "total" | "breakdown";
  range?: TimelineRange;
  onRangeChange?: (range: TimelineRange) => void;
  onCompositionChange?: (mode: "total" | "breakdown") => void;
}

export function NetWorthCard({
  className,
  overview,
  embedded = false,
  composition,
  range,
  onRangeChange,
  onCompositionChange,
}: NetWorthCardProps) {
  const [internalRange, setInternalRange] = useState<TimelineRange>("1Y");
  const [internalComposition, setInternalComposition] = useState<"total" | "breakdown">("total");

  const effectiveRange = range ?? internalRange;
  const setEffectiveRange = onRangeChange ?? setInternalRange;

  const effectiveComposition = composition ?? internalComposition;
  const setEffectiveComposition = onCompositionChange ?? setInternalComposition;

  const { hidden } = useBalanceHidden();

  // 1. Fetch Headline Summary
  const summaryQuery = useQuery({
    queryKey: ["net-worth", "summary"],
    queryFn: () => getNetWorthSummary(),
  });

  // 2. Fetch Historical Timeline
  const timelineQuery = useQuery({
    queryKey: ["net-worth", "timeline", { range: effectiveRange }],
    queryFn: () => getNetWorthTimeline(effectiveRange),
  });

  const summary = summaryQuery.data;
  const timeline = timelineQuery.data;
  const points = timeline?.points ?? [];

  // Executive Wealth Vitals
  const savingsRatePct = useMemo(() => {
    if (!overview || overview.income_paise <= 0) return null;
    const surplus = overview.income_paise + overview.expense_paise;
    return Math.round((surplus / overview.income_paise) * 1000) / 10;
  }, [overview]);

  const netPeerDues = useMemo(() => {
    if (!overview) return null;
    const rec = overview.peer_receivables_paise ?? 0;
    const pay = overview.peer_payables_paise ?? 0;
    if (rec === 0 && pay === 0) return null;
    return rec - pay;
  }, [overview]);

  // Transform timeline points from paise to rupees for charting
  // Liabilities are kept strictly in accordance with PRD §F8 View 4: bank overdrafts + net peer payables
  const chartData = useMemo(() => {
    return points.map((p) => {
      return {
        date: p.date,
        net_worth: p.net_worth_paise / 100,
        bank_cash: Math.max(0, p.bank_cash_paise) / 100,
        indian_mf: p.indian_mf_paise / 100,
        indian_equity: p.indian_equity_paise / 100,
        us_equity: p.us_equity_paise / 100,
        debt_fd: p.debt_fd_paise / 100,
        gold: p.gold_paise / 100,
        peer_receivables: p.peer_receivables_paise / 100,
        raw_net_worth_paise: p.net_worth_paise,
        liabilities: -(p.liabilities_paise / 100),
        raw_liabilities_paise: p.liabilities_paise,
      };
    });
  }, [points]);

  // Omit liability overlay line if all values in selected range are 0
  const hasLiabilities = useMemo(() => {
    return chartData.some((d) => d.raw_liabilities_paise > 0);
  }, [chartData]);

  // Determine which milestone guides are relevant
  const maxNetWorthRupees = useMemo(() => {
    if (chartData.length === 0) return 0;
    return Math.max(...chartData.map((d) => d.net_worth));
  }, [chartData]);

  const activeMilestones = useMemo(() => {
    return MILESTONES.filter(
      (m) => m.value <= maxNetWorthRupees * 1.35 || (maxNetWorthRupees > 0 && m.value <= maxNetWorthRupees),
    );
  }, [maxNetWorthRupees]);

  const yAxisConfig = useMemo(() => {
    if (effectiveComposition === "total") {
      return {
        domain: [(dataMin: number) => Math.min(0, dataMin), "auto"] as [
          (dataMin: number) => number,
          string,
        ],
        ticks: undefined,
      };
    }

    // Breakdown mode: liabilities line may be visible
    const minLiab = chartData.reduce((min, d) => Math.min(min, d.liabilities), 0);
    if (!hasLiabilities || minLiab >= 0) {
      return {
        domain: [(dataMin: number) => Math.min(0, dataMin), "auto"] as [
          (dataMin: number) => number,
          string,
        ],
        ticks: undefined,
      };
    }

    // Has negative liabilities in breakdown mode: clamp domain so -2k doesn't create a -6.0L floor
    const { ticks, yMax } = getNiceTicks(maxNetWorthRupees, 4);
    const yMin = minLiab * 1.2;
    return {
      domain: [yMin, yMax] as [number, number],
      ticks,
    };
  }, [chartData, effectiveComposition, hasLiabilities, maxNetWorthRupees]);

  const nwPaise = summary?.net_worth_paise ?? 0;
  const assetsPaise = summary?.assets_paise ?? 0;
  const liabilitiesPaise = summary?.liabilities_paise ?? 0;
  const delta30dPaise = summary?.delta_30d_paise ?? 0;
  const delta30dPct = summary?.delta_30d_pct;
  const delta1yPaise = summary?.delta_1y_paise ?? 0;
  const delta1yPct = summary?.delta_1y_pct;

  // Shared Headline Stats Content
  const headlineStats = (
    <div>
      <div className="flex flex-wrap items-baseline gap-3">
        <div
          className="text-[32px] font-bold tracking-tight text-foreground sm:text-[36px]"
          style={{ fontVariantNumeric: "tabular-nums lining-nums" }}
        >
          {summary ? (
            <Sensitive>
              {nwPaise < 0 ? "−" : ""}
              {formatINR(Math.abs(nwPaise))}
            </Sensitive>
          ) : (
            <span className="text-muted-foreground">—</span>
          )}
        </div>

        {/* 30-Day Delta Pill */}
        {summary && delta30dPaise !== 0 ? (
          <div
            className={cn(
              "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11.5px] font-medium",
              delta30dPaise >= 0
                ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                : "bg-rose-500/10 text-rose-600 dark:text-rose-400",
            )}
          >
            {delta30dPaise >= 0 ? (
              <TrendingUpIcon className="h-3 w-3" />
            ) : (
              <TrendingDownIcon className="h-3 w-3" />
            )}
            <Sensitive>
              {delta30dPaise > 0 ? "+" : "−"}
              {formatINR(Math.abs(delta30dPaise))}
            </Sensitive>
            {delta30dPct !== null && delta30dPct !== undefined && (
              <span>({delta30dPct > 0 ? "+" : ""}{delta30dPct}%)</span>
            )}
            <span className="text-muted-foreground/80 font-normal">30d</span>
          </div>
        ) : null}

        {/* 1-Year Delta Pill */}
        {summary && delta1yPaise !== 0 ? (
          <div className="inline-flex items-center gap-1 text-[11.5px] text-muted-foreground">
            <span>1y:</span>
            <Sensitive>
              {delta1yPaise > 0 ? "+" : "−"}
              {formatINR(Math.abs(delta1yPaise))}
            </Sensitive>
            {delta1yPct !== null && delta1yPct !== undefined && (
              <span>({delta1yPct > 0 ? "+" : ""}{delta1yPct}%)</span>
            )}
          </div>
        ) : null}
      </div>

      {/* Asset vs Liability Decomposition */}
      {summary ? (
        <p className="mt-1 text-[12px] text-muted-foreground">
          Liquid Assets <Sensitive>{formatINR(summary.breakdown?.bank_cash_paise ?? assetsPaise)}</Sensitive>
          {" · "}
          Investments <Sensitive>
            {formatINR(
              (summary.breakdown?.indian_equity_paise ?? 0) +
              (summary.breakdown?.indian_mf_paise ?? 0) +
              (summary.breakdown?.us_equity_paise ?? 0) +
              (summary.breakdown?.debt_fd_paise ?? 0) +
              (summary.breakdown?.gold_paise ?? 0),
            )}
          </Sensitive>
          {(summary.breakdown?.peer_receivables_paise ?? 0) > 0 ? (
            <>
              {" · "}
              Receivables <Sensitive className="text-emerald-600 dark:text-emerald-400">+{formatINR(summary.breakdown.peer_receivables_paise)}</Sensitive>
            </>
          ) : null}
          {liabilitiesPaise > 0 ? (
            <>
              {" · "}
              Liabilities <Sensitive className="text-rose-600 dark:text-rose-400">−{formatINR(liabilitiesPaise)}</Sensitive>
            </>
          ) : null}
        </p>
      ) : null}

      {/* Executive Wealth Vitals Strip */}
      {overview && (savingsRatePct !== null || netPeerDues !== null) ? (
        <div className="mt-2.5 flex flex-wrap items-center gap-2">
          {savingsRatePct !== null && (
            <div className="inline-flex items-center gap-1 rounded-md border border-emerald-500/20 bg-emerald-500/10 px-2 py-0.5 text-[11px] font-medium text-emerald-600 dark:text-emerald-400">
              <span>Savings Rate:</span>
              <span className="font-semibold">{savingsRatePct}%</span>
            </div>
          )}
          {netPeerDues !== null && netPeerDues !== 0 && (
            <Link
              href="/friends"
              className={cn(
                "inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] font-medium transition-colors hover:underline",
                netPeerDues > 0
                  ? "border-emerald-500/20 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                  : "border-rose-500/20 bg-rose-500/10 text-rose-600 dark:text-rose-400",
              )}
            >
              <span>Khata Dues:</span>
              <span className="font-semibold font-mono">
                <Sensitive>
                  {netPeerDues > 0 ? "+" : "−"}{formatINR(Math.abs(netPeerDues))}
                </Sensitive>
              </span>
            </Link>
          )}
        </div>
      ) : null}

      {overview && overview.fx_unavailable_count > 0 ? (
        <p className="mt-1.5 text-[11px] text-amber-600 dark:text-amber-400">
          ⚠ Excludes {overview.fx_unavailable_count} USD holding
          {overview.fx_unavailable_count === 1 ? "" : "s"} — no USD→INR rate is cached.
        </p>
      ) : null}
    </div>
  );

  return (
    <Card
      className={cn(
        "overflow-hidden",
        embedded ? "border-0 bg-transparent shadow-none" : "border bg-card",
        className,
      )}
    >
      {!embedded ? (
        <CardHeader className="flex flex-col gap-4 pb-2 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <div className="flex items-center gap-2">
              <Eyebrow as="h2">Net Worth Timeline & Wealth Trajectory</Eyebrow>
              <span className="flex items-center gap-1 text-[10.5px] font-medium text-emerald-600 dark:text-emerald-400">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse" />
                Live
              </span>
              <Link
                href="/portfolio"
                className="ml-2 inline-flex items-center gap-0.5 text-[11px] font-medium text-muted-foreground hover:text-foreground hover:underline"
              >
                Portfolio <ArrowUpRightIcon className="h-3 w-3" />
              </Link>
            </div>
            <div className="mt-2">{headlineStats}</div>
          </div>

          <div className="flex flex-col items-end gap-2">
            {/* View Composition Toggle in Standalone Mode */}
            <div className="flex items-center rounded-md border border-border/60 bg-muted/40 p-0.5">
              <Button
                variant="ghost"
                size="sm"
                aria-pressed={effectiveComposition === "total"}
                onClick={() => setEffectiveComposition("total")}
                className={cn(
                  "h-6 px-2.5 text-[11px] font-medium transition-colors",
                  effectiveComposition === "total"
                    ? "bg-background text-foreground shadow-xs font-semibold"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                Total
              </Button>
              <Button
                variant="ghost"
                size="sm"
                aria-pressed={effectiveComposition === "breakdown"}
                onClick={() => setEffectiveComposition("breakdown")}
                className={cn(
                  "h-6 px-2.5 text-[11px] font-medium transition-colors",
                  effectiveComposition === "breakdown"
                    ? "bg-background text-foreground shadow-xs font-semibold"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                Breakdown
              </Button>
            </div>

            {/* Range Selector Pill Bar in Standalone Mode */}
            <div className="flex items-center rounded-md border bg-muted/40 p-0.5">
              {RANGES.map((r) => (
                <Button
                  key={r}
                  variant="ghost"
                  size="sm"
                  aria-pressed={effectiveRange === r}
                  onClick={() => setEffectiveRange(r)}
                  className={cn(
                    "h-7 px-2.5 text-[11.5px] font-medium transition-colors",
                    effectiveRange === r
                      ? "bg-background text-foreground shadow-xs font-semibold"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  {r}
                </Button>
              ))}
            </div>
          </div>
        </CardHeader>
      ) : null}

      <CardContent className={cn("pt-2", embedded && "px-4 pt-3")}>
        {/* In embedded mode, render the headline stats cleanly at top of card content */}
        {embedded ? <div className="mb-3">{headlineStats}</div> : null}

        {timelineQuery.isPending ? (
          <div className="flex h-[280px] items-center justify-center text-sm text-muted-foreground">
            Loading trajectory curve…
          </div>
        ) : timelineQuery.isError ? (
          <div className="flex h-[280px] items-center justify-center text-sm text-neg">
            Unable to compute net worth timeline.
          </div>
        ) : chartData.length === 0 ? (
          <div className="flex h-[280px] items-center justify-center text-sm text-muted-foreground">
            No historical account or investment records in this period.
          </div>
        ) : (
          <ChartContainer
            config={CHART_CONFIG}
            className={cn(
              "h-[280px] w-full aspect-auto transition-all duration-200",
              hidden && "pointer-events-none select-none blur-sm",
            )}
          >
            <ComposedChart
              data={chartData}
              margin={{ top: 12, right: 16, left: 0, bottom: 4 }}
            >
              <defs>
                <linearGradient id="grad-net-worth" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#10b981" stopOpacity={0.35} />
                  <stop offset="95%" stopColor="#10b981" stopOpacity={0.02} />
                </linearGradient>
                <linearGradient id="grad-bank" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#0ea5e9" stopOpacity={0.4} />
                  <stop offset="95%" stopColor="#0ea5e9" stopOpacity={0.05} />
                </linearGradient>
                <linearGradient id="grad-mf" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#8b5cf6" stopOpacity={0.4} />
                  <stop offset="95%" stopColor="#8b5cf6" stopOpacity={0.05} />
                </linearGradient>
                <linearGradient id="grad-equity" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.4} />
                  <stop offset="95%" stopColor="#3b82f6" stopOpacity={0.05} />
                </linearGradient>
                <linearGradient id="grad-us" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#ec4899" stopOpacity={0.4} />
                  <stop offset="95%" stopColor="#ec4899" stopOpacity={0.05} />
                </linearGradient>
                <linearGradient id="grad-debt" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#64748b" stopOpacity={0.4} />
                  <stop offset="95%" stopColor="#64748b" stopOpacity={0.05} />
                </linearGradient>
                <linearGradient id="grad-gold" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#eab308" stopOpacity={0.4} />
                  <stop offset="95%" stopColor="#eab308" stopOpacity={0.05} />
                </linearGradient>
                <linearGradient id="grad-peer" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#14b8a6" stopOpacity={0.4} />
                  <stop offset="95%" stopColor="#14b8a6" stopOpacity={0.05} />
                </linearGradient>
              </defs>

              <CartesianGrid strokeDasharray="3 3" vertical={false} opacity={0.25} />

              <XAxis
                dataKey="date"
                tickLine={false}
                axisLine={false}
                tickMargin={8}
                minTickGap={28}
                interval="preserveStartEnd"
                fontSize={11}
                tickFormatter={(val: string) => {
                  try {
                    const d = new Date(val);
                    if (effectiveRange === "1M" || effectiveRange === "6M") {
                      return d.toLocaleDateString("en-IN", {
                        day: "numeric",
                        month: "short",
                      });
                    }
                    return d.toLocaleDateString("en-IN", {
                      month: "short",
                      year: "2-digit",
                    });
                  } catch {
                    return val;
                  }
                }}
              />

              <YAxis
                domain={yAxisConfig.domain}
                ticks={yAxisConfig.ticks}
                tickLine={false}
                axisLine={false}
                tickMargin={8}
                fontSize={11}
                tickFormatter={(val: number) => (hidden ? "••••" : compactINR(val * 100))}
              />

              {/* Milestone Guides */}
              {activeMilestones.map((m) => (
                <ReferenceLine
                  key={m.label}
                  y={m.value}
                  stroke="#94a3b8"
                  strokeDasharray="4 4"
                  strokeOpacity={0.6}
                  label={{
                    value: m.label,
                    position: "insideTopRight",
                    fill: "#64748b",
                    fontSize: 10,
                  }}
                />
              ))}

              {/* Subtle y=0 baseline when liabilities exist below zero in breakdown mode */}
              {effectiveComposition === "breakdown" && hasLiabilities && (
                <ReferenceLine y={0} stroke="#94a3b8" strokeOpacity={0.3} />
              )}

              {/* Chart Modes: Total (single smooth area) vs Breakdown (7 asset classes) */}
              {effectiveComposition === "total" ? (
                <Area
                  type="monotone"
                  dataKey="net_worth"
                  name="Net Worth"
                  stroke="#10b981"
                  strokeWidth={2}
                  fill="url(#grad-net-worth)"
                  isAnimationActive={false}
                />
              ) : (
                <>
                  <Area
                    type="monotone"
                    dataKey="bank_cash"
                    name="Cash & Bank"
                    stackId="assets"
                    stroke="#0ea5e9"
                    fill="url(#grad-bank)"
                    isAnimationActive={false}
                  />
                  <Area
                    type="monotone"
                    dataKey="indian_mf"
                    name="Mutual Funds"
                    stackId="assets"
                    stroke="#8b5cf6"
                    fill="url(#grad-mf)"
                    isAnimationActive={false}
                  />
                  <Area
                    type="monotone"
                    dataKey="indian_equity"
                    name="Equity"
                    stackId="assets"
                    stroke="#3b82f6"
                    fill="url(#grad-equity)"
                    isAnimationActive={false}
                  />
                  <Area
                    type="monotone"
                    dataKey="us_equity"
                    name="US Stocks"
                    stackId="assets"
                    stroke="#ec4899"
                    fill="url(#grad-us)"
                    isAnimationActive={false}
                  />
                  <Area
                    type="monotone"
                    dataKey="debt_fd"
                    name="Fixed Income"
                    stackId="assets"
                    stroke="#64748b"
                    fill="url(#grad-debt)"
                    isAnimationActive={false}
                  />
                  <Area
                    type="monotone"
                    dataKey="gold"
                    name="Gold"
                    stackId="assets"
                    stroke="#eab308"
                    fill="url(#grad-gold)"
                    isAnimationActive={false}
                  />
                  <Area
                    type="monotone"
                    dataKey="peer_receivables"
                    name="Peer Receivables"
                    stackId="assets"
                    stroke="#14b8a6"
                    fill="url(#grad-peer)"
                    isAnimationActive={false}
                  />

                  {/* Total Net Worth Curve Overlay in Breakdown Mode */}
                  <Line
                    type="monotone"
                    dataKey="net_worth"
                    name="Net Worth"
                    stroke="#10b981"
                    strokeWidth={2.5}
                    dot={false}
                    activeDot={{ r: 4, stroke: "#10b981", strokeWidth: 2, fill: "#ffffff" }}
                    isAnimationActive={false}
                  />
                </>
              )}

              {/* In breakdown mode, distinct #f43f5e dashed line overlay for liabilities (PRD §F8 View 4) */}
              {effectiveComposition === "breakdown" && hasLiabilities && (
                <Line
                  type="monotone"
                  dataKey="liabilities"
                  name="Liabilities"
                  stroke="#f43f5e"
                  strokeWidth={1.5}
                  strokeDasharray="4 3"
                  dot={false}
                  activeDot={{ r: 3, stroke: "#f43f5e", strokeWidth: 1.5, fill: "#ffffff" }}
                  isAnimationActive={false}
                />
              )}

              <ChartTooltip
                content={({ active, payload }) => {
                  if (!active || !payload || payload.length === 0) return null;
                  const item = payload[0]?.payload;
                  if (!item) return null;
                  return (
                    <div className="rounded-lg border bg-background/95 p-2.5 shadow-lg backdrop-blur-sm text-[12px]">
                      <div className="font-semibold text-foreground pb-1 border-b border-border/40">
                        {item.date}
                      </div>
                      <div className="mt-1.5 flex items-center justify-between gap-4 font-bold text-emerald-600 dark:text-emerald-400">
                        <span>Net Worth:</span>
                        <span>
                          {hidden ? "••••" : formatINR(item.raw_net_worth_paise)}
                        </span>
                      </div>

                      {item.raw_liabilities_paise > 0 && (
                        <div className="mt-1 flex items-center justify-between gap-4 font-medium text-rose-500">
                          <span className="flex items-center gap-1.5">
                            <span className="h-2 w-2 rounded-full bg-[#f43f5e]" />
                            Liabilities:
                          </span>
                          <span>
                            {hidden ? "••••" : `−${formatINR(item.raw_liabilities_paise)}`}
                          </span>
                        </div>
                      )}

                      {effectiveComposition === "breakdown" ? (
                        <div className="mt-1 flex flex-col gap-0.5 text-muted-foreground text-[11px] border-t border-border/30 pt-1">
                          {item.bank_cash > 0 && (
                            <div className="flex justify-between gap-3">
                              <span className="flex items-center gap-1.5">
                                <span className="h-2 w-2 rounded-full bg-[#0ea5e9]" />
                                Cash & Bank:
                              </span>
                              <span>{hidden ? "••••" : formatINR(item.bank_cash * 100)}</span>
                            </div>
                          )}
                          {item.indian_mf > 0 && (
                            <div className="flex justify-between gap-3">
                              <span className="flex items-center gap-1.5">
                                <span className="h-2 w-2 rounded-full bg-[#8b5cf6]" />
                                Mutual Funds:
                              </span>
                              <span>{hidden ? "••••" : formatINR(item.indian_mf * 100)}</span>
                            </div>
                          )}
                          {item.indian_equity > 0 && (
                            <div className="flex justify-between gap-3">
                              <span className="flex items-center gap-1.5">
                                <span className="h-2 w-2 rounded-full bg-[#3b82f6]" />
                                Equity:
                              </span>
                              <span>{hidden ? "••••" : formatINR(item.indian_equity * 100)}</span>
                            </div>
                          )}
                          {item.us_equity > 0 && (
                            <div className="flex justify-between gap-3">
                              <span className="flex items-center gap-1.5">
                                <span className="h-2 w-2 rounded-full bg-[#ec4899]" />
                                US Stocks:
                              </span>
                              <span>{hidden ? "••••" : formatINR(item.us_equity * 100)}</span>
                            </div>
                          )}
                          {item.debt_fd > 0 && (
                            <div className="flex justify-between gap-3">
                              <span className="flex items-center gap-1.5">
                                <span className="h-2 w-2 rounded-full bg-[#64748b]" />
                                Fixed Income:
                              </span>
                              <span>{hidden ? "••••" : formatINR(item.debt_fd * 100)}</span>
                            </div>
                          )}
                          {item.gold > 0 && (
                            <div className="flex justify-between gap-3">
                              <span className="flex items-center gap-1.5">
                                <span className="h-2 w-2 rounded-full bg-[#eab308]" />
                                Gold:
                              </span>
                              <span>{hidden ? "••••" : formatINR(item.gold * 100)}</span>
                            </div>
                          )}
                          {item.peer_receivables > 0 && (
                            <div className="flex justify-between gap-3">
                              <span className="flex items-center gap-1.5">
                                <span className="h-2 w-2 rounded-full bg-[#14b8a6]" />
                                Peer Receivables:
                              </span>
                              <span>{hidden ? "••••" : formatINR(item.peer_receivables * 100)}</span>
                            </div>
                          )}
                        </div>
                      ) : null}
                    </div>
                  );
                }}
              />
            </ComposedChart>
          </ChartContainer>
        )}

        {/* Legend */}
        <div className="mt-3 flex flex-wrap items-center justify-center gap-x-4 gap-y-1.5 border-t border-border/40 px-4 pt-3 text-[11px] text-muted-foreground">
          {effectiveComposition === "total" ? (
            <span className="flex items-center gap-1.5">
              <span className="h-2 w-2 rounded-full bg-[#10b981]" />
              Net Worth
            </span>
          ) : (
            <>
              <span className="flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-full bg-[#0ea5e9]" />
                Cash & Bank
              </span>
              <span className="flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-full bg-[#8b5cf6]" />
                Mutual Funds
              </span>
              <span className="flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-full bg-[#3b82f6]" />
                Equity
              </span>
              <span className="flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-full bg-[#ec4899]" />
                US Stocks
              </span>
              <span className="flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-full bg-[#64748b]" />
                Fixed Income
              </span>
              <span className="flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-full bg-[#eab308]" />
                Gold
              </span>
              <span className="flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-full bg-[#14b8a6]" />
                Peer Receivables
              </span>
              {hasLiabilities && (
                <span className="flex items-center gap-1.5 text-rose-500">
                  <span className="inline-block h-0.5 w-3 border-t-2 border-dashed border-[#f43f5e]" />
                  Liabilities
                </span>
              )}
            </>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
