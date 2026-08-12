"use client";

/**
 * Weekly / monthly spend bar (PRD §F8 view 3). Toggles between a trailing
 * 13-week and trailing 12-month aggregation of `GET /dashboards/spend-by-period`,
 * whose `total_paise` is signed (spend negative) — negated to a positive
 * magnitude for the bars, net-credit periods floored to 0 (matching the
 * SummaryStrip sparkline; a spend bar shouldn't render negative).
 *
 * Owns its own window + grain, independent of view 2's month stepper. The
 * week-grain window is the SHARED `trailingWeeksWindow` (lib/dates) that the
 * SummaryStrip sparkline also uses, so both `spend-by-period` week queries
 * resolve to one TanStack cache key instead of double-fetching the series.
 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ReferenceLine,
  XAxis,
  YAxis,
} from "recharts";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { listSpendByPeriod } from "@/lib/api/client";
import { compactINR, formatINR } from "@/lib/format";
import { periodLabel, type Grain } from "@/lib/charts";
import { periodRange } from "@/lib/period";
import { cn } from "@/lib/utils";
import { Sensitive, useBalanceHidden } from "@/components/balance-visibility";

const CHART_CONFIG = {
  spend: { label: "Spend", color: "var(--chart-1)" },
} satisfies ChartConfig;

export function SpendByPeriodBar({
  labelId,
  year,
}: {
  labelId?: number;
  year: number;
}) {
  const [grain, setGrain] = useState<Grain>("week");
  const { hidden } = useBalanceHidden();

  const { start, end } = periodRange({ year });

  const query = useQuery({
    queryKey: [
      "dashboards",
      "spend-by-period",
      { bucket: grain, start, end, label_id: labelId },
    ],
    queryFn: () =>
      listSpendByPeriod({ bucket: grain, start, end, label_id: labelId }),
  });

  // Trim leading pre-history: buckets before the first with any activity.
  const buckets = query.data?.buckets ?? [];
  const firstActive = buckets.findIndex((b) => b.total_paise !== 0);
  const trimmed = firstActive === -1 ? buckets : buckets.slice(firstActive);

  const data = trimmed.map((b) => ({
    label: periodLabel(b.period, grain),
    spend: Math.max(0, -b.total_paise),
  }));

  // Trailing-average baseline over the 4 most-recent COMPLETE periods.
  const completeSpends = data.slice(0, -1).map((d) => d.spend);
  const avgWindow = completeSpends.slice(-4);
  const trailingAvg =
    avgWindow.length === 4
      ? avgWindow.reduce((sum, s) => sum + s, 0) / 4
      : null;

  const mid = Math.ceil(data.length / 2);
  const leftMax = Math.max(0, ...data.slice(0, mid).map((d) => d.spend));
  const rightMax = Math.max(0, ...data.slice(mid).map((d) => d.spend));
  const annotateRight = leftMax > rightMax;

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between border-b">
        <div className="flex items-center gap-2">
          <CardTitle as="h2" className="text-[14px]">
            Spend over time
          </CardTitle>
          <span className="text-[12.5px] text-muted-foreground">· {year}</span>
        </div>
        <div className="flex items-center gap-1">
          {(["week", "month"] as const).map((g) => (
            <Button
              key={g}
              type="button"
              variant={grain === g ? "secondary" : "ghost"}
              onClick={() => setGrain(g)}
              className="h-7 px-2.5 text-[12px] capitalize"
            >
              {g === "week" ? "Weekly" : "Monthly"}
            </Button>
          ))}
        </div>
      </CardHeader>

      <CardContent className="pt-4">
        {query.isError ? (
          <p className="py-8 text-center text-[13px] text-neg">
            Couldn’t load — is the API running?
          </p>
        ) : (
          <div className="relative">
            {trailingAvg != null && trailingAvg > 0 ? (
              <div
                className={cn(
                  "pointer-events-none absolute top-1 z-10 flex items-center gap-1.5 rounded-md border border-border bg-background/85 px-2 py-1 text-[11px] text-muted-foreground shadow-sm",
                  annotateRight ? "right-2" : "left-[56px]",
                )}
              >
                <span
                  aria-hidden
                  className="inline-block w-3.5 border-t border-dashed border-muted-foreground"
                />
                {grain === "week" ? "4-wk avg" : "4-mo avg"}
                <span
                  className="tabular-nums text-foreground/80"
                  style={{ fontVariantNumeric: "tabular-nums lining-nums" }}
                >
                  <Sensitive>{compactINR(trailingAvg)}</Sensitive>
                </span>
              </div>
            ) : null}
            <ChartContainer
              config={CHART_CONFIG}
              className={cn(
                "aspect-auto h-[240px] w-full",
                hidden && "pointer-events-none select-none blur-sm",
              )}
            >
              <BarChart data={data} margin={{ left: 4, right: 4, top: 4 }}>
                <CartesianGrid vertical={false} />
                <XAxis
                  dataKey="label"
                  tickLine={false}
                  axisLine={false}
                  tickMargin={8}
                  minTickGap={grain === "week" ? 16 : 8}
                  className="text-[10px]"
                />
                <YAxis
                  tickLine={false}
                  axisLine={false}
                  width={48}
                  tickFormatter={(v: number) => compactINR(v)}
                  className="text-[10px]"
                />
                <ChartTooltip
                  content={
                    <ChartTooltipContent
                      formatter={(value) => formatINR(Number(value))}
                    />
                  }
                />
                <Bar dataKey="spend" fill="var(--color-spend)" radius={3} maxBarSize={grain === "week" ? 14 : 32} />
                {trailingAvg != null && trailingAvg > 0 ? (
                  <ReferenceLine
                    y={trailingAvg}
                    stroke="var(--muted-foreground)"
                    strokeDasharray="4 4"
                    strokeWidth={1}
                  />
                ) : null}
              </BarChart>
            </ChartContainer>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
