"use client";

/**
 * Portfolio Performance Chart (Milestone 2 Phase 5).
 *
 * Interactive rebased performance chart showing portfolio Time-Weighted Return (TWR)
 * vs. Benchmark TRI line starting at 100.0 (Index) or 0.0% (Return).
 * Supports rolling period selection ("1M", "3M", "6M", "1Y", "3Y", "5Y", "all"),
 * visual dormant liquidation gap indicators, and detailed tooltips.
 */

import { useMemo, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceArea,
  ReferenceLine,
  XAxis,
  YAxis,
} from "recharts";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  ChartContainer,
  ChartTooltip,
  type ChartConfig,
} from "@/components/ui/chart";
import { Pill } from "@/components/ui/pill";
import { formatPercent } from "@/lib/format";
import {
  filterPointsByPeriod,
  rebaseTimeseriesPoints,
  ROLLING_PERIODS,
  type RollingPeriod,
} from "@/lib/investments";
import type {
  PeriodPerformance,
  PortfolioTimeseriesPoint,
} from "@/lib/api/portfolio-timeseries";
import { cn } from "@/lib/utils";

const CHART_CONFIG = {
  portfolio: {
    label: "Portfolio (TWR)",
    color: "hsl(var(--primary))",
  },
  benchmark: {
    label: "Benchmark (TRI)",
    color: "#d97706",
  },
} satisfies ChartConfig;

export type PortfolioPerformanceChartProps = {
  points: PortfolioTimeseriesPoint[];
  periods?: PeriodPerformance[];
  benchmarkName?: string | null;
  selectedPeriod: RollingPeriod;
  onSelectPeriod: (period: RollingPeriod) => void;
  className?: string;
};

export function PortfolioPerformanceChart({
  points,
  periods,
  benchmarkName = "Benchmark",
  selectedPeriod,
  onSelectPeriod,
  className,
}: PortfolioPerformanceChartProps) {
  const [displayMode, setDisplayMode] = useState<"pct" | "base100">("pct");

  // 1. Filter points for the selected rolling period
  const scopedPoints = useMemo(
    () => filterPointsByPeriod(points, selectedPeriod, periods),
    [points, selectedPeriod, periods],
  );

  // 2. Rebase portfolio TWR and benchmark to 0.0% or 100.0 index
  const chartData = useMemo(
    () => rebaseTimeseriesPoints(scopedPoints, displayMode),
    [scopedPoints, displayMode],
  );

  // 3. Identify dormant / uninvested intervals for ReferenceArea shading
  const dormantIntervals = useMemo(() => {
    const intervals: Array<{ start: string; end: string }> = [];
    let start: string | null = null;

    chartData.forEach((pt, i) => {
      if (!pt.isInvested) {
        if (!start) start = pt.date;
        if (i === chartData.length - 1 && start) {
          intervals.push({ start, end: pt.date });
        }
      } else if (start) {
        intervals.push({ start, end: chartData[i - 1].date });
        start = null;
      }
    });

    return intervals;
  }, [chartData]);

  const hasData = chartData.length > 0;
  const isAllZeroOrSingle =
    chartData.length <= 1 ||
    chartData.every((pt) => pt.portfolioReturn === 0 && (pt.benchmarkReturn === 0 || pt.benchmarkReturn == null));

  // Determine current active period performance summary if available
  const activePeriodMeta = periods?.find((p) => p.period === selectedPeriod);

  return (
    <Card className={cn("overflow-hidden", className)}>
      <CardHeader className="flex flex-col gap-3 pb-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <CardTitle className="text-base font-semibold">
              Performance (TWR vs. Benchmark)
            </CardTitle>
            {activePeriodMeta?.twr_pct != null ? (
              <Badge
                variant="outline"
                size="sm"
                className={cn(
                  "font-mono text-[11px]",
                  activePeriodMeta.twr_pct >= 0
                    ? "text-pos border-pos/30 bg-pos/10"
                    : "text-neg border-neg/30 bg-neg/10",
                )}
              >
                {activePeriodMeta.twr_pct >= 0 ? "+" : ""}
                {formatPercent(activePeriodMeta.twr_pct)}
              </Badge>
            ) : null}
          </div>
          <p className="text-[12px] text-muted-foreground mt-0.5">
            Unitized Time-Weighted Return stripped of cash-flow distortions vs.{" "}
            {benchmarkName ?? "Index Fund"}
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {/* Base 100 vs % Return toggle */}
          <div className="flex items-center rounded-lg border border-border p-0.5 bg-muted/40">
            <button
              type="button"
              onClick={() => setDisplayMode("pct")}
              className={cn(
                "rounded-md px-2 py-0.5 text-[11px] font-medium transition-colors",
                displayMode === "pct"
                  ? "bg-background text-foreground shadow-xs"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              % Return
            </button>
            <button
              type="button"
              onClick={() => setDisplayMode("base100")}
              className={cn(
                "rounded-md px-2 py-0.5 text-[11px] font-medium transition-colors",
                displayMode === "base100"
                  ? "bg-background text-foreground shadow-xs"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              Base 100
            </button>
          </div>

          {/* Time range selector pills */}
          <div className="flex items-center gap-1">
            {ROLLING_PERIODS.map((p) => (
              <Pill
                key={p}
                size="sm"
                active={selectedPeriod === p}
                onClick={() => onSelectPeriod(p)}
                className="cursor-pointer text-[11px] px-2"
                aria-pressed={selectedPeriod === p}
              >
                {p === "all" ? "All" : p}
              </Pill>
            ))}
          </div>
        </div>
      </CardHeader>

      <CardContent className="pt-2">
        {!hasData ? (
          <div className="flex h-[280px] w-full items-center justify-center rounded-lg border border-dashed border-border text-[13px] text-muted-foreground">
            No timeseries points recorded for this period.
          </div>
        ) : (
          <div className="relative">
            <ChartContainer
              config={CHART_CONFIG}
              className="aspect-auto h-[290px] w-full"
            >
              <LineChart
                syncId="portfolio-hub-timeline"
                data={chartData}
                margin={{ left: 8, right: 12, top: 12, bottom: 4 }}
              >
                <CartesianGrid vertical={false} strokeDasharray="3 3" />
                <XAxis
                  dataKey="date"
                  tickLine={false}
                  axisLine={false}
                  tickMargin={8}
                  minTickGap={28}
                  tickFormatter={(d: string) => {
                    const parts = d.split("-");
                    if (parts.length >= 3) {
                      const m = parseInt(parts[1], 10);
                      const monthNames = [
                        "",
                        "Jan",
                        "Feb",
                        "Mar",
                        "Apr",
                        "May",
                        "Jun",
                        "Jul",
                        "Aug",
                        "Sep",
                        "Oct",
                        "Nov",
                        "Dec",
                      ];
                      return `${monthNames[m]} '${parts[0].slice(2)}`;
                    }
                    return d;
                  }}
                  className="text-[10px]"
                />
                <YAxis
                  tickLine={false}
                  axisLine={false}
                  width={46}
                  domain={isAllZeroOrSingle ? [-2, 2] : ["auto", "auto"]}
                  tickFormatter={(v: number) =>
                    displayMode === "pct"
                      ? `${v > 0 ? "+" : ""}${Math.round(v)}%`
                      : `${Math.round(v)}`
                  }
                  className="text-[10px] font-mono"
                />

                {/* Base reference line */}
                <ReferenceLine
                  y={displayMode === "pct" ? 0 : 100}
                  stroke="var(--border)"
                  strokeWidth={1.5}
                />

                {/* Dormant / uninvested periods shading */}
                {dormantIntervals.map((interval) => (
                  <ReferenceArea
                    key={`${interval.start}-${interval.end}`}
                    x1={interval.start}
                    x2={interval.end}
                    fill="currentColor"
                    fillOpacity={0.06}
                    stroke="var(--border)"
                    strokeDasharray="3 3"
                  />
                ))}

                <ChartTooltip
                  content={({ active, payload }) => {
                    if (!active || !payload?.length) return null;
                    const data = payload[0].payload;
                    const pReturn = data.portfolioReturn as number;
                    const bReturn = data.benchmarkReturn as number | null;
                    const dReturn = data.dailyReturn as number;
                    const isDormant = !data.isInvested;

                    return (
                      <div className="rounded-lg border border-border bg-card p-2.5 shadow-md text-xs min-w-[210px] space-y-1.5">
                        <div className="flex items-center justify-between border-b border-border/60 pb-1 font-mono text-[11px] text-muted-foreground">
                          <span>{data.date}</span>
                          {isDormant ? (
                            <span className="rounded bg-amber-500/10 px-1 py-0.2 text-[10px] text-amber-600 dark:text-amber-400 font-sans">
                              Dormant / Uninvested
                            </span>
                          ) : (
                            <span className="rounded bg-emerald-500/10 px-1 py-0.2 text-[10px] text-emerald-600 dark:text-emerald-400 font-sans">
                              Active
                            </span>
                          )}
                        </div>

                        <div className="flex items-center justify-between">
                          <span className="flex items-center gap-1.5 text-foreground/80">
                            <span className="size-2 rounded-full bg-primary" />
                            Portfolio (TWR):
                          </span>
                          <span
                            className={cn(
                              "font-mono font-semibold tabular-nums",
                              pReturn > 0
                                ? "text-pos"
                                : pReturn < 0
                                  ? "text-neg"
                                  : "text-foreground",
                            )}
                          >
                            {displayMode === "pct"
                              ? `${pReturn >= 0 ? "+" : ""}${pReturn.toFixed(2)}%`
                              : pReturn.toFixed(2)}
                          </span>
                        </div>

                        {bReturn != null ? (
                          <div className="flex items-center justify-between">
                            <span className="flex items-center gap-1.5 text-muted-foreground">
                              <span className="size-2 rounded-full bg-amber-500" />
                              {benchmarkName ?? "Benchmark"}:
                            </span>
                            <span
                              className={cn(
                                "font-mono font-medium tabular-nums",
                                bReturn > 0
                                  ? "text-pos"
                                  : bReturn < 0
                                    ? "text-neg"
                                    : "text-foreground",
                              )}
                            >
                              {displayMode === "pct"
                                ? `${bReturn >= 0 ? "+" : ""}${bReturn.toFixed(2)}%`
                                : bReturn.toFixed(2)}
                            </span>
                          </div>
                        ) : null}

                        <div className="flex items-center justify-between border-t border-border/40 pt-1 text-[10.5px] text-muted-foreground">
                          <span>Daily Return:</span>
                          <span
                            className={cn(
                              "font-mono tabular-nums",
                              dReturn > 0
                                ? "text-pos"
                                : dReturn < 0
                                  ? "text-neg"
                                  : "text-muted-foreground",
                            )}
                          >
                            {dReturn >= 0 ? "+" : ""}
                            {(dReturn * 100).toFixed(2)}%
                          </span>
                        </div>
                      </div>
                    );
                  }}
                />

                {/* Portfolio TWR Line */}
                <Line
                  dataKey="portfolioReturn"
                  name="Portfolio"
                  type="monotone"
                  stroke="var(--color-portfolio)"
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />

                {/* Benchmark Line */}
                <Line
                  dataKey="benchmarkReturn"
                  name="Benchmark"
                  type="monotone"
                  stroke="var(--color-benchmark)"
                  strokeWidth={1.75}
                  strokeDasharray="4 2"
                  dot={false}
                  isAnimationActive={false}
                />
              </LineChart>
            </ChartContainer>

            {dormantIntervals.length > 0 ? (
              <div className="mt-2 flex items-center gap-2 text-[11px] text-muted-foreground">
                <span className="inline-block h-2 w-3 rounded-xs border border-border bg-foreground/10" />
                <span>
                  Shaded zones indicate dormant periods (zero active holdings / liquidation gap).
                </span>
              </div>
            ) : null}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
