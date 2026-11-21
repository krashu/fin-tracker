"use client";

/**
 * Portfolio Valuation & Capital Curve Chart (Milestone 2 Phase 5).
 *
 * Dual-series Area/Line chart comparing Portfolio Value (INR) against Net Invested Capital (INR).
 * Includes an optional toggle to display the Cambridge Associates mPME counterfactual value curve.
 * Respects rupee privacy masking via `useBalanceHidden()`.
 */

import { useMemo, useState } from "react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  XAxis,
  YAxis,
} from "recharts";

import { useBalanceHidden, Sensitive } from "@/components/balance-visibility";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  ChartContainer,
  ChartTooltip,
  type ChartConfig,
} from "@/components/ui/chart";
import { compactINR, formatINR } from "@/lib/format";
import {
  filterPointsByPeriod,
  type RollingPeriod,
} from "@/lib/investments";
import type {
  PeriodPerformance,
  PortfolioTimeseriesPoint,
} from "@/lib/api/portfolio-timeseries";
import { cn } from "@/lib/utils";

const CHART_CONFIG = {
  value: {
    label: "Portfolio Value",
    color: "hsl(var(--primary))",
  },
  invested: {
    label: "Net Invested Capital",
    color: "#64748b",
  },
  mpme: {
    label: "Benchmark mPME Counterfactual",
    color: "#d97706",
  },
} satisfies ChartConfig;

export type ValuationCapitalChartProps = {
  points: PortfolioTimeseriesPoint[];
  periods?: PeriodPerformance[];
  benchmarkName?: string | null;
  selectedPeriod: RollingPeriod;
  className?: string;
};

export function ValuationCapitalChart({
  points,
  periods,
  benchmarkName = "Benchmark",
  selectedPeriod,
  className,
}: ValuationCapitalChartProps) {
  const { hidden } = useBalanceHidden();
  const [showMpme, setShowMpme] = useState(true);

  // Filter points for the selected period
  const scopedPoints = useMemo(
    () => filterPointsByPeriod(points, selectedPeriod, periods),
    [points, selectedPeriod, periods],
  );

  // Convert paise to rupees for charting
  const chartData = useMemo(() => {
    return scopedPoints.map((p) => ({
      date: p.date,
      value: p.portfolio_value_paise / 100,
      invested: p.invested_capital_paise / 100,
      mpme:
        p.benchmark_value_paise != null
          ? p.benchmark_value_paise / 100
          : null,
      gainPaise: p.portfolio_value_paise - p.invested_capital_paise,
    }));
  }, [scopedPoints]);

  const latestPoint = chartData[chartData.length - 1];
  const hasData = chartData.length > 0;

  return (
    <Card className={cn("overflow-hidden", className)}>
      <CardHeader className="flex flex-col gap-3 pb-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <CardTitle className="text-base font-semibold">
              Valuation & Capital Curve
            </CardTitle>
            {latestPoint ? (
              <Badge variant="outline" size="sm" className="font-mono text-[11px]">
                <Sensitive>{formatINR(latestPoint.value * 100)}</Sensitive>
              </Badge>
            ) : null}
          </div>
          <p className="text-[12px] text-muted-foreground mt-0.5">
            Realized portfolio growth against cumulative net capital invested
          </p>
        </div>

        <div className="flex items-center gap-2">
          {/* Toggle Cambridge Associates mPME counterfactual curve */}
          <button
            type="button"
            onClick={() => setShowMpme((prev) => !prev)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-[11.5px] font-medium transition-colors cursor-pointer",
              showMpme
                ? "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300"
                : "border-border bg-card text-muted-foreground hover:bg-muted hover:text-foreground",
            )}
            aria-pressed={showMpme}
          >
            <span
              className={cn(
                "size-2 rounded-full",
                showMpme ? "bg-amber-500" : "bg-muted-foreground/40",
              )}
            />
            <span>mPME ({benchmarkName})</span>
          </button>
        </div>
      </CardHeader>

      <CardContent className="pt-2">
        {!hasData ? (
          <div className="flex h-[280px] w-full items-center justify-center rounded-lg border border-dashed border-border text-[13px] text-muted-foreground">
            No valuation points recorded for this period.
          </div>
        ) : (
          <div className="relative">
            <ChartContainer
              config={CHART_CONFIG}
              className={cn(
                "aspect-auto h-[290px] w-full",
                hidden && "pointer-events-none select-none blur-sm",
              )}
            >
              <ComposedChart
                syncId="portfolio-hub-timeline"
                data={chartData}
                margin={{ left: 8, right: 12, top: 12, bottom: 4 }}
              >
                <defs>
                  <linearGradient id="valGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop
                      offset="5%"
                      stopColor="var(--color-value)"
                      stopOpacity={0.2}
                    />
                    <stop
                      offset="95%"
                      stopColor="var(--color-value)"
                      stopOpacity={0.0}
                    />
                  </linearGradient>
                </defs>

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
                  width={54}
                  tickFormatter={(v: number) => compactINR(v)}
                  className="text-[10px] font-mono"
                />

                <ChartTooltip
                  content={({ active, payload }) => {
                    if (!active || !payload?.length) return null;
                    const d = payload[0].payload;
                    const val = d.value as number;
                    const inv = d.invested as number;
                    const mpmeVal = d.mpme as number | null;
                    const gain = d.gainPaise as number;

                    return (
                      <div className="rounded-lg border border-border bg-card p-2.5 shadow-md text-xs min-w-[220px] space-y-1.5">
                        <div className="border-b border-border/60 pb-1 font-mono text-[11px] text-muted-foreground">
                          {d.date}
                        </div>

                        <div className="flex items-center justify-between">
                          <span className="flex items-center gap-1.5 text-foreground/80">
                            <span className="size-2 rounded-full bg-primary" />
                            Portfolio Value:
                          </span>
                          <span className="font-mono font-semibold tabular-nums text-foreground">
                            {formatINR(val * 100)}
                          </span>
                        </div>

                        <div className="flex items-center justify-between">
                          <span className="flex items-center gap-1.5 text-muted-foreground">
                            <span className="size-2 rounded-full bg-slate-500" />
                            Invested Capital:
                          </span>
                          <span className="font-mono tabular-nums text-muted-foreground">
                            {formatINR(inv * 100)}
                          </span>
                        </div>

                        <div className="flex items-center justify-between border-t border-border/40 pt-1 text-[11px]">
                          <span className="text-muted-foreground">Unrealized Gain:</span>
                          <span
                            className={cn(
                              "font-mono font-medium tabular-nums",
                              gain > 0
                                ? "text-pos"
                                : gain < 0
                                  ? "text-neg"
                                  : "text-foreground",
                            )}
                          >
                            {gain >= 0 ? "+" : ""}
                            {formatINR(gain)}
                          </span>
                        </div>

                        {showMpme && mpmeVal != null ? (
                          <div className="flex items-center justify-between border-t border-border/40 pt-1 text-[11px]">
                            <span className="flex items-center gap-1 text-amber-600 dark:text-amber-400">
                              <span className="size-1.5 rounded-full bg-amber-500" />
                              mPME Counterfactual:
                            </span>
                            <span className="font-mono tabular-nums text-amber-600 dark:text-amber-400">
                              {formatINR(mpmeVal * 100)}
                            </span>
                          </div>
                        ) : null}
                      </div>
                    );
                  }}
                />

                {/* Net Invested Capital Line */}
                <Line
                  dataKey="invested"
                  name="Invested"
                  type="stepAfter"
                  stroke="var(--color-invested)"
                  strokeWidth={1.75}
                  strokeDasharray="3 3"
                  dot={false}
                  isAnimationActive={false}
                />

                {/* Benchmark mPME Line */}
                {showMpme ? (
                  <Line
                    dataKey="mpme"
                    name="Benchmark mPME"
                    type="monotone"
                    stroke="var(--color-mpme)"
                    strokeWidth={1.5}
                    strokeDasharray="4 2"
                    dot={false}
                    isAnimationActive={false}
                  />
                ) : null}

                {/* Portfolio Value Area */}
                <Area
                  dataKey="value"
                  name="Value"
                  type="monotone"
                  stroke="var(--color-value)"
                  strokeWidth={2}
                  fill="url(#valGrad)"
                  dot={false}
                  isAnimationActive={false}
                />
              </ComposedChart>
            </ChartContainer>

            <div className="mt-2 flex flex-wrap items-center gap-4 text-[11px] text-muted-foreground">
              <span className="flex items-center gap-1.5">
                <span className="size-2 rounded-full bg-primary" />
                Portfolio Value
              </span>
              <span className="flex items-center gap-1.5">
                <span className="h-0.5 w-3 bg-slate-500" />
                Net Invested Capital (cash in/out)
              </span>
              {showMpme ? (
                <span className="flex items-center gap-1.5 text-amber-600 dark:text-amber-400">
                  <span className="h-0.5 w-3 bg-amber-500" />
                  Cambridge Associates mPME ({benchmarkName})
                </span>
              ) : null}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
