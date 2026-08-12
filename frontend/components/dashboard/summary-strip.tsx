"use client";

/**
 * /expenses summary strip — shows yearly and monthly spend + income totals,
 * driven by the board's filter state (year, month, allDates). When a month is
 * selected, both yearly and monthly rows are shown; when "All dates" (= full
 * year) is active, only the yearly row is shown.
 *
 * Values are unsigned magnitudes (always positive on screen). `expense_paise`
 * from `period-totals` is signed (≤ 0 typically); we negate it for display.
 * Income is always ≥ 0.
 *
 * The 14-week trailing sparkline is always anchored to "today" — it does NOT
 * follow the filter state. It answers "what's my recent spending velocity?"
 * regardless of what year/month the table is showing.
 */
import { useQuery } from "@tanstack/react-query";

import { listPeriodTotals, listSpendByPeriod } from "@/lib/api/client";
import { monthRange, trailingWeeksWindow } from "@/lib/dates";
import { formatINR, formatMonthYear } from "@/lib/format";
import { periodRange } from "@/lib/period";
import { cn } from "@/lib/utils";
import { Sensitive, useBalanceHidden } from "@/components/balance-visibility";

export type SummaryStripProps = {
  year?: number;
  monthAnchor?: Date;
  allDates?: boolean;
};

export function SummaryStrip({
  year = new Date().getFullYear(),
  monthAnchor = new Date(new Date().getFullYear(), new Date().getMonth(), 1),
  allDates = false,
}: SummaryStripProps = {}) {
  const now = new Date();
  const weekly = trailingWeeksWindow(now, 13);
  const { hidden } = useBalanceHidden();

  // ── Yearly totals ─────────────────────────────────────────────────────
  const { start: yearStart, end: yearEnd } = periodRange({ year });

  const yearTotalsQ = useQuery({
    queryKey: ["dashboards", "period-totals", { start: yearStart, end: yearEnd }],
    queryFn: () => listPeriodTotals({ start: yearStart, end: yearEnd }),
    // Mirrors monthTotalsQ's `enabled: !allDates` below — only one of the two
    // totals is ever rendered (see `totalsQ` further down), so the other was
    // fetching for nothing.
    enabled: allDates,
  });

  const yearSpend = -(yearTotalsQ.data?.expense_paise ?? 0);
  const yearIncome = yearTotalsQ.data?.income_paise ?? 0;

  // ── Monthly totals (only when a specific month is selected) ───────────
  const { date_from: monthStart, date_to: monthEnd } = monthRange(monthAnchor);
  const monthLabel = formatMonthYear(monthAnchor);

  const monthTotalsQ = useQuery({
    queryKey: [
      "dashboards",
      "period-totals",
      { start: monthStart, end: monthEnd },
    ],
    queryFn: () => listPeriodTotals({ start: monthStart, end: monthEnd }),
    enabled: !allDates,
  });

  const monthSpend = -(monthTotalsQ.data?.expense_paise ?? 0);
  const monthIncome = monthTotalsQ.data?.income_paise ?? 0;

  // ── 14-week sparkline (always trailing from today) ────────────────────
  const weeklyQuery = useQuery({
    queryKey: ["dashboards", "spend-by-period", { bucket: "week", ...weekly }],
    queryFn: () => listSpendByPeriod({ bucket: "week", ...weekly }),
  });

  const sparkData = (weeklyQuery.data?.buckets ?? []).map((b) =>
    Math.max(0, -b.total_paise),
  );

  // Choose which totals to display based on allDates:
  // - allDates true ("All months"): show yearly totals
  // - allDates false (specific month): show monthly totals only
  const showLabel = allDates ? String(year) : monthLabel;
  const totalsQ = allDates ? yearTotalsQ : monthTotalsQ;
  const spend = allDates ? yearSpend : monthSpend;
  const income = allDates ? yearIncome : monthIncome;

  return (
    <section className="flex flex-wrap items-center gap-x-6 gap-y-2 border-b border-border pt-4 pb-5">
      <div className="flex items-baseline gap-2">
        <span
          className="text-[10.5px] font-medium uppercase text-muted-foreground"
          style={{ letterSpacing: "0.13em" }}
        >
          Spend · {showLabel}
        </span>
        <span
          className={cn(
            "text-[28px] font-semibold leading-none tracking-[-0.024em] tabular-nums",
            totalsQ.isSuccess
              ? "text-foreground"
              : "text-muted-foreground",
          )}
          style={{ fontVariantNumeric: "tabular-nums lining-nums" }}
        >
          {totalsQ.isSuccess ? (
            <Sensitive>{formatINR(Math.abs(spend))}</Sensitive>
          ) : (
            "—"
          )}
        </span>
      </div>

      <div className="flex items-baseline gap-2">
        <span
          className="text-[10.5px] font-medium uppercase text-muted-foreground"
          style={{ letterSpacing: "0.13em" }}
        >
          Income · {showLabel}
        </span>
        <span
          className={cn(
            "text-[20px] font-semibold leading-none tracking-[-0.02em] tabular-nums",
            totalsQ.isSuccess
              ? "text-foreground"
              : "text-muted-foreground",
          )}
          style={{ fontVariantNumeric: "tabular-nums lining-nums" }}
        >
          {totalsQ.isSuccess ? (
            <Sensitive>{formatINR(income)}</Sensitive>
          ) : (
            "—"
          )}
        </span>
      </div>

      <div className={cn("ml-auto", hidden && "select-none blur-[3px]")}>
        <Sparkline data={sparkData} />
      </div>
    </section>
  );
}

function Sparkline({ data }: { data: number[] }) {
  const w = 80;
  const h = 32;
  const n = data.length;
  if (n === 0)
    return <svg width={w} height={h} aria-label="Last 14 weeks of spending" />;

  const gap = 1;
  const barW = (w - gap * (n - 1)) / n;
  const max = Math.max(1, ...data);

  return (
    <svg width={w} height={h} aria-label="Last 14 weeks of spending">
      {data.map((v, i) => {
        const bh = Math.max(2, (v / max) * (h - 2));
        const x = i * (barW + gap);
        const y = h - bh;
        const isLast = i === n - 1;
        return (
          <rect
            key={i}
            x={x}
            y={y}
            width={barW}
            height={bh}
            rx={0.5}
            className={isLast ? "fill-primary" : "fill-muted-foreground"}
            opacity={isLast ? 1 : 0.55}
          />
        );
      })}
    </svg>
  );
}
