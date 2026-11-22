"use client";

/**
 * Cashflow Inflow-to-Wealth Waterfall Card (PRD §F8 View 4 & Wealth Analytics).
 *
 * Visualizes the annual conversion of cash inflow to wealth:
 * Gross Income -> Living Expenses (by top parent categories) -> Peer Lending/Repayments
 * -> Net Investment Transfers -> Net Liquid Surplus.
 *
 * Supports calendar-year and financial-year scopes with privacy masking.
 */

import { useMemo, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
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
import { useAvailableYears } from "@/components/dashboard/use-available-years";
import {
  getCashflowWaterfall,
  type YearType,
} from "@/lib/api/client";
import { compactINR, formatINR } from "@/lib/format";
import { currentYear } from "@/lib/period";
import { cn } from "@/lib/utils";

const CHART_CONFIG = {
  amount: { label: "Cashflow", color: "#3b82f6" },
} satisfies ChartConfig;

export function CashflowWaterfallCard({
  className,
  embedded = false,
}: {
  className?: string;
  embedded?: boolean;
}) {
  const { hidden } = useBalanceHidden();
  const yearsQuery = useAvailableYears();
  const [selectedYear, setSelectedYear] = useState<number>(() => currentYear());
  const [yearType, setYearType] = useState<YearType>("calendar");

  const effectiveYear = selectedYear ?? yearsQuery.years[0] ?? currentYear();

  const waterfallQuery = useQuery({
    queryKey: ["dashboards", "cashflow-waterfall", { year: effectiveYear, year_type: yearType }],
    queryFn: () => getCashflowWaterfall(effectiveYear, yearType),
  });

  const data = waterfallQuery.data;
  const steps = data?.steps ?? [];

  // Transform steps into waterfall stacked segments [base, amount]
  // Consolidates living expenses into Top 5 + Other Living if >5 expense categories
  const chartData = useMemo(() => {
    if (steps.length === 0) return [];

    let processedSteps = steps;
    const expenseSteps = steps.filter((s) => s.kind === "expense");
    if (expenseSteps.length > 5) {
      const sortedExp = [...expenseSteps].sort(
        (a, b) => Math.abs(b.amount_paise) - Math.abs(a.amount_paise),
      );
      const top5 = new Set(sortedExp.slice(0, 5).map((e) => e.label));
      const otherExpenses = sortedExp.slice(5);
      const otherAmount = otherExpenses.reduce((sum, e) => sum + e.amount_paise, 0);

      let runningCum = 0;
      const newSteps: typeof steps = [];
      let otherAdded = false;

      for (const s of steps) {
        if (s.kind === "income") {
          runningCum += s.amount_paise;
          newSteps.push({ ...s, step_index: newSteps.length, cumulative_paise: runningCum });
        } else if (s.kind === "expense") {
          if (top5.has(s.label)) {
            runningCum += s.amount_paise;
            newSteps.push({ ...s, step_index: newSteps.length, cumulative_paise: runningCum });
          } else if (!otherAdded) {
            runningCum += otherAmount;
            newSteps.push({
              step_index: newSteps.length,
              label: "Other Living",
              amount_paise: otherAmount,
              cumulative_paise: runningCum,
              kind: "expense",
              display_color: "#94a3b8",
            });
            otherAdded = true;
          }
        } else if (s.kind === "surplus") {
          newSteps.push({ ...s, step_index: newSteps.length, cumulative_paise: runningCum });
        } else {
          runningCum += s.amount_paise;
          newSteps.push({ ...s, step_index: newSteps.length, cumulative_paise: runningCum });
        }
      }
      processedSteps = newSteps;
    }

    return processedSteps.map((s) => {
      let baseRupees = 0;
      const amtRupees = Math.abs(s.amount_paise) / 100;
      const cumRupees = s.cumulative_paise / 100;

      if (s.kind === "income" || s.kind === "surplus") {
        baseRupees = 0;
      } else {
        // Outflows step downwards: base starts at the lower level
        if (s.amount_paise <= 0) {
          baseRupees = cumRupees;
        } else {
          // Inflow step upwards
          baseRupees = cumRupees - amtRupees;
        }
      }

      return {
        label: s.label,
        base: Math.max(0, baseRupees),
        amount: amtRupees,
        signed_amount_paise: s.amount_paise,
        cumulative_paise: s.cumulative_paise,
        kind: s.kind,
        color:
          s.display_color ||
          (s.kind === "income"
            ? "#10b981"
            : s.kind === "surplus"
            ? "#06b6d4"
            : s.kind === "investment"
            ? "#3b82f6"
            : s.amount_paise < 0
            ? "#ef4444"
            : "#10b981"),
      };
    });
  }, [steps]);

  const grossIncome = data?.gross_income_paise ?? 0;
  const livingExpenses = data?.total_living_expenses_paise ?? 0;
  const netSurplus = data?.net_liquid_surplus_paise ?? 0;

  return (
    <Card
      className={cn(
        "overflow-hidden",
        embedded ? "border-0 bg-transparent shadow-none" : "border bg-card",
        className,
      )}
    >
      <CardHeader className="flex flex-col gap-3 pb-2 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Eyebrow as="h2">Cashflow Waterfall: Inflow to Wealth</Eyebrow>
            <Link
              href="/spending"
              className="inline-flex items-center gap-0.5 text-[11px] font-medium text-muted-foreground hover:text-foreground hover:underline"
            >
              Category spending ↗
            </Link>
          </div>

          <div className="mt-1 flex flex-wrap items-baseline gap-4">
            <div className="flex items-center gap-1.5 text-[13px] font-semibold text-foreground">
              <span className="text-muted-foreground font-normal">Income:</span>
              <Sensitive className="text-emerald-600 dark:text-emerald-400">
                {formatINR(grossIncome)}
              </Sensitive>
            </div>

            <div className="flex items-center gap-1.5 text-[13px] font-semibold text-foreground">
              <span className="text-muted-foreground font-normal">Living Spend:</span>
              <Sensitive className="text-rose-600 dark:text-rose-400">
                {livingExpenses < 0 ? "−" : ""}
                {formatINR(Math.abs(livingExpenses))}
              </Sensitive>
            </div>

            <div className="flex items-center gap-1.5 text-[13px] font-semibold text-foreground">
              <span className="text-muted-foreground font-normal">Liquid Surplus:</span>
              <Sensitive
                className={
                  netSurplus >= 0
                    ? "text-cyan-600 dark:text-cyan-400"
                    : "text-amber-600 dark:text-amber-400"
                }
              >
                {netSurplus < 0 ? "−" : ""}
                {formatINR(Math.abs(netSurplus))}
              </Sensitive>
            </div>
          </div>
        </div>

        {/* Controls: Year Type + Year Picker */}
        <div className="flex flex-wrap items-center gap-2">
          {/* Calendar vs Fiscal Year Toggle */}
          <div className="flex items-center rounded-md border bg-muted/40 p-0.5">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setYearType("calendar")}
              className={cn(
                "h-7 px-2 text-[11px] font-medium transition-colors",
                yearType === "calendar"
                  ? "bg-background text-foreground shadow-xs font-semibold"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              Calendar (CY)
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setYearType("fiscal")}
              className={cn(
                "h-7 px-2 text-[11px] font-medium transition-colors",
                yearType === "fiscal"
                  ? "bg-background text-foreground shadow-xs font-semibold"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              Fiscal (FY)
            </Button>
          </div>

          {/* Year Picker */}
          <select
            value={effectiveYear}
            onChange={(e) => setSelectedYear(Number(e.target.value))}
            className="h-8 rounded-md border border-input bg-background px-2 py-1 text-[12px] font-medium text-foreground shadow-xs focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          >
            {yearsQuery.years.map((y) => (
              <option key={y} value={y}>
                {yearType === "fiscal" ? `FY ${y}-${(y + 1) % 100}` : y}
              </option>
            ))}
          </select>
        </div>
      </CardHeader>

      <CardContent className="pt-2">
        {waterfallQuery.isPending ? (
          <div className="flex h-[260px] items-center justify-center text-sm text-muted-foreground">
            Computing waterfall decomposition…
          </div>
        ) : waterfallQuery.isError ? (
          <div className="flex h-[260px] items-center justify-center text-sm text-neg">
            Unable to load cashflow waterfall.
          </div>
        ) : chartData.length === 0 ? (
          <div className="flex h-[260px] items-center justify-center text-sm text-muted-foreground">
            No income or spending records found for this year.
          </div>
        ) : (
          <ChartContainer config={CHART_CONFIG} className="h-[260px] w-full">
            <BarChart
              data={chartData}
              margin={{ top: 12, right: 12, left: 0, bottom: 20 }}
            >
              <CartesianGrid strokeDasharray="3 3" vertical={false} opacity={0.2} />

              <XAxis
                dataKey="label"
                tickLine={false}
                axisLine={false}
                tickMargin={8}
                fontSize={10.5}
                interval={0}
                angle={0}
                textAnchor="middle"
              />

              <YAxis
                tickLine={false}
                axisLine={false}
                tickMargin={8}
                fontSize={11}
                tickFormatter={(val: number) => (hidden ? "••••" : compactINR(val * 100))}
              />

              <ReferenceLine y={0} stroke="#94a3b8" opacity={0.5} />

              {/* Invisible transparent base segment for floating waterfall bars */}
              <Bar dataKey="base" stackId="waterfall" fill="transparent" />

              {/* Visible floating bar for each step */}
              <Bar dataKey="amount" stackId="waterfall" radius={[3, 3, 3, 3]}>
                {chartData.map((entry, index) => (
                  <Cell key={`cell-${index}`} fill={entry.color} />
                ))}
              </Bar>

              <ChartTooltip
                content={({ active, payload }) => {
                  if (!active || !payload || payload.length === 0) return null;
                  const item = payload[0]?.payload;
                  if (!item) return null;
                  return (
                    <div className="rounded-lg border bg-background/95 p-2.5 shadow-lg backdrop-blur-sm text-[12px]">
                      <div className="font-semibold text-foreground pb-1 border-b border-border/40">
                        {item.label}
                      </div>
                      <div className="mt-1 flex items-center justify-between gap-4 font-bold">
                        <span className="text-muted-foreground font-normal">Change:</span>
                        <span
                          style={{
                            color: item.signed_amount_paise >= 0 ? "#10b981" : "#ef4444",
                          }}
                        >
                          {hidden
                            ? "••••"
                            : `${item.signed_amount_paise > 0 ? "+" : item.signed_amount_paise < 0 ? "−" : ""}${formatINR(
                                Math.abs(item.signed_amount_paise),
                              )}`}
                        </span>
                      </div>
                      <div className="mt-0.5 flex items-center justify-between gap-4 text-[11px] text-muted-foreground">
                        <span>Cumulative Level:</span>
                        <span className="font-medium text-foreground">
                          {hidden ? "••••" : formatINR(item.cumulative_paise)}
                        </span>
                      </div>
                    </div>
                  );
                }}
              />
            </BarChart>
          </ChartContainer>
        )}
      </CardContent>
    </Card>
  );
}
