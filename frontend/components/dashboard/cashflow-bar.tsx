"use client";

/**
 * Income vs spend + net cashflow (PRD §F8 view 3 — the /spending "am I solvent"
 * chart). Trailing 12 calendar months of `GET /dashboards/cashflow-by-period`:
 * grouped income (green) / spend (red) bars drawn as positive magnitudes, plus a
 * net-cashflow line (`income + expense`, signed) that dips below the break-even
 * `y=0` reference on a deficit month.
 *
 * Monthly grain only — income typically lands once a month, so weekly buckets
 * would be mostly income=0 noise. Owns its own window, independent of the other
 * /spending cards (each child holds its own period state).
 */
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  XAxis,
  YAxis,
} from "recharts";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { listCashflowByPeriod } from "@/lib/api/client";
import { compactINR, formatINR } from "@/lib/format";
import { periodLabel } from "@/lib/charts";
import { periodRange } from "@/lib/period";
import { cn } from "@/lib/utils";
import { useBalanceHidden } from "@/components/balance-visibility";

const CHART_CONFIG = {
  income: { label: "Income", color: "var(--pos)" },
  spend: { label: "Spend", color: "var(--neg)" },
  net: { label: "Net", color: "var(--foreground)" },
} satisfies ChartConfig;

// `tagActive` = the /spending tag filter is set. CashflowBar is deliberately NOT
// tag-scoped: labels apply to spend rows only (a refund included — ADR-0009),
// so filtering would zero out all income and collapse "am I solvent" into a
// flat income line. Instead it stays whole-account and shows a caption saying
// so (below) — honest rather than silently ignoring the shared selector.
export function CashflowBar({ tagActive = false, year }: { tagActive?: boolean; year: number }) {
  const { hidden } = useBalanceHidden();

  const { start, end } = periodRange({ year });
  const query = useQuery({
    queryKey: [
      "dashboards",
      "cashflow-by-period",
      { bucket: "month", start, end },
    ],
    queryFn: () => listCashflowByPeriod({ bucket: "month", start, end }),
  });

  const data = (query.data?.buckets ?? []).map((b) => ({
    label: periodLabel(b.period, "month"),
    income: b.income_paise, // ≥ 0
    spend: Math.max(0, -b.expense_paise),
    net: b.net_paise, // signed — dips below the y=0 reference on a deficit month
  }));

  return (
    <Card>
      <CardHeader className="border-b">
        <div className="flex items-center gap-2">
          <CardTitle as="h2" className="text-[14px]">
            Income vs spend
          </CardTitle>
          <span className="text-[12.5px] text-muted-foreground">· {year}</span>
        </div>
        {/* Static caption independent of query state — this card ignores the
            tag filter on purpose (income can't be tagged). */}
        {tagActive ? (
          <p className="text-[11.5px] text-muted-foreground">
            Whole-account — tags apply to spending only.
          </p>
        ) : null}
      </CardHeader>

      <CardContent className="pt-4">
        {query.isError ? (
          <p className="py-8 text-center text-[13px] text-neg">
            Couldn’t load — is the API running?
          </p>
        ) : (
          <ChartContainer
            config={CHART_CONFIG}
            className={cn(
              "aspect-auto h-[260px] w-full",
              // Blur the bars/line + Y-axis amounts and disable the tooltip so no
              // magnitude leaks while balances are hidden.
              hidden && "pointer-events-none select-none blur-sm",
            )}
          >
            <ComposedChart data={data} margin={{ left: 4, right: 4, top: 4 }}>
              <CartesianGrid vertical={false} />
              <XAxis
                dataKey="label"
                tickLine={false}
                axisLine={false}
                tickMargin={8}
                minTickGap={8}
                className="text-[10px]"
              />
              <YAxis
                tickLine={false}
                axisLine={false}
                width={48}
                tickFormatter={(v: number) => compactINR(v)}
                className="text-[10px]"
              />
              {/* Break-even baseline: obvious when the net line crosses below. */}
              <ReferenceLine y={0} stroke="var(--border)" strokeWidth={1} />
              <ChartTooltip
                content={
                  <ChartTooltipContent
                    // With 3 series the row NAME matters — but ChartTooltipContent
                    // hands the whole row to `formatter` (chart.tsx:215), dropping
                    // its default swatch + label. So rebuild the row here: color
                    // dot (via the config-injected --color-<key>) + label + INR.
                    formatter={(value, name) => {
                      const key = String(name) as keyof typeof CHART_CONFIG;
                      return (
                        <div className="flex w-full items-center justify-between gap-3">
                          <span className="flex items-center gap-1.5 text-muted-foreground">
                            <span
                              className="h-2.5 w-2.5 shrink-0 rounded-[2px]"
                              style={{ backgroundColor: `var(--color-${key})` }}
                            />
                            {CHART_CONFIG[key]?.label ?? name}
                          </span>
                          <span className="font-mono font-medium tabular-nums text-foreground">
                            {formatINR(Number(value))}
                          </span>
                        </div>
                      );
                    }}
                  />
                }
              />
              <ChartLegend content={<ChartLegendContent />} />
              <Bar dataKey="income" fill="var(--color-income)" radius={3} />
              <Bar dataKey="spend" fill="var(--color-spend)" radius={3} />
              <Line
                dataKey="net"
                type="monotone"
                stroke="var(--color-net)"
                strokeWidth={2}
                dot={false}
              />
            </ComposedChart>
          </ChartContainer>
        )}
      </CardContent>
    </Card>
  );
}
