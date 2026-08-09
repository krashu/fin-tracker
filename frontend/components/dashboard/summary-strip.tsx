"use client";

/**
 * /expenses summary anchor (PRD §F8 spend bar) — the live replacement for the
 * old static mock strip. Shows the CURRENT month's spend total, a
 * month-over-month delta vs the SAME day-span last month, and a weekly spend
 * sparkline. All three come from `GET /dashboards/spend-by-period`, whose
 * `total_paise` is signed (spend negative, refund positive — PRD §F4a); we
 * negate to a positive spend magnitude for display.
 *
 * This is an INDEPENDENT monthly anchor: it derives its own windows from
 * "today" and is NOT driven by the board's FilterRow. It DOES react to
 * transaction mutations (the bulk bar / row dialog invalidate `["dashboards"]`),
 * because an edit/delete changes the month total (PRD §F9) and a mounted query
 * won't refetch on staleTime alone.
 */
import { useQuery } from "@tanstack/react-query";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { listPeriodTotals, listSpendByPeriod } from "@/lib/api/client";
import { toLocalYMD, trailingWeeksWindow } from "@/lib/dates";
import {
  formatDateRange,
  formatINR,
  formatINRWhole,
  formatMonthYear,
} from "@/lib/format";
import { cn } from "@/lib/utils";
import { Sensitive, useBalanceHidden } from "@/components/balance-visibility";

// -----------------------------------------------------------------------------
// Month-window builders, co-located here (summary-specific MoM math, distinct
// from filter-row's presetRange). Local Y/M/D via toLocalYMD — never
// toISOString() (IST +5:30 would shift the day back and mis-window the query).
// The weekly window is the shared `trailingWeeksWindow` (lib/dates) so the
// sparkline and the /spending period bar share one cache entry.
// -----------------------------------------------------------------------------
type Window = { start: string; end: string };

/** [first of this month .. today] — current month-to-date. */
function currentMonthWindow(now: Date): Window {
  const first = new Date(now.getFullYear(), now.getMonth(), 1);
  return { start: toLocalYMD(first), end: toLocalYMD(now) };
}

/** [first of last month .. same day-span last month] — apples-to-apples MoM.
 * Day is clamped to last month's length (Mar 31 -> Feb 28). JS Date normalizes
 * the month underflow (month -1 in January -> December of the prior year). */
function priorSamePeriodWindow(now: Date): Window {
  const first = new Date(now.getFullYear(), now.getMonth() - 1, 1);
  // Day 0 of the current month = last day of the previous month.
  const daysInPrevMonth = new Date(
    now.getFullYear(),
    now.getMonth(),
    0,
  ).getDate();
  const day = Math.min(now.getDate(), daysInPrevMonth);
  const end = new Date(first.getFullYear(), first.getMonth(), day);
  return { start: toLocalYMD(first), end: toLocalYMD(end) };
}

export function SummaryStrip() {
  // Capture "now" ONCE so the label and all three windows agree even on a
  // render that straddles midnight (the derived YMD strings are stable within
  // a day, so the query keys don't churn across renders).
  const now = new Date();
  const current = currentMonthWindow(now);
  const prior = priorSamePeriodWindow(now);
  const weekly = trailingWeeksWindow(now, 13);
  const { hidden } = useBalanceHidden();

  const currentQuery = useQuery({
    queryKey: [
      "dashboards",
      "spend-by-period",
      { bucket: "month", ...current },
    ],
    queryFn: () => listSpendByPeriod({ bucket: "month", ...current }),
  });
  const priorQuery = useQuery({
    queryKey: ["dashboards", "spend-by-period", { bucket: "month", ...prior }],
    queryFn: () => listSpendByPeriod({ bucket: "month", ...prior }),
  });
  const weeklyQuery = useQuery({
    queryKey: ["dashboards", "spend-by-period", { bucket: "week", ...weekly }],
    queryFn: () => listSpendByPeriod({ bucket: "week", ...weekly }),
  });
  // Month income, surfaced beside spend. Own stat, no MoM delta: the spend
  // delta's "down is good / green" semantics are inverted for income, so a
  // shared delta would mislead. Shares the `["dashboards"]` invalidation prefix.
  const incomeQuery = useQuery({
    queryKey: ["dashboards", "period-totals", { ...current }],
    queryFn: () => listPeriodTotals(current),
  });
  const monthIncome = incomeQuery.data?.income_paise ?? 0;

  // Negate the signed sum to a positive spend magnitude. A month-window query
  // returns exactly one bucket, so buckets[0] is the period total.
  const currentSpend = -(currentQuery.data?.buckets[0]?.total_paise ?? 0);
  const priorSpend = -(priorQuery.data?.buckets[0]?.total_paise ?? 0);

  // Delta only renders when both month queries resolved. % needs a positive
  // prior to divide by; at exactly 0% (or no prior spend) there's no arrow.
  const showDelta = currentQuery.isSuccess && priorQuery.isSuccess;
  const deltaPct =
    priorSpend > 0 ? ((currentSpend - priorSpend) / priorSpend) * 100 : null;
  const direction =
    deltaPct === null
      ? null
      : deltaPct < 0
        ? "down"
        : deltaPct > 0
          ? "up"
          : "flat";
  // Spending down is good (green); up is bad (red); flat/none is neutral.
  const deltaColor =
    direction === "down"
      ? "text-pos"
      : direction === "up"
        ? "text-neg"
        : "text-muted-foreground";

  // Spell out the apples-to-apples comparison windows so "▼84.8%" can't misread
  // as "vs all of last month". Built from the same `current`/`prior` windows the
  // queries use; formatDateRange decides the year once per range, so a label
  // spanning a year boundary stays symmetric (both ends carry the year).
  const deltaWindowLabel =
    `${formatDateRange(current.start, current.end)} vs ` +
    `${formatDateRange(prior.start, prior.end)} · same period last month`;

  // Sparkline: floor net-credit weeks to 0 — a spend bar shouldn't render a
  // negative height (don't "fix" this into a signed bar).
  const sparkData = (weeklyQuery.data?.buckets ?? []).map((b) =>
    Math.max(0, -b.total_paise),
  );

  // `pb-7` (not the symmetric `py-4`) is a UX-15 MITIGATION, not the fix. On
  // /expenses the board's sticky filter row sits directly under this hairline,
  // ~40px from the hero number, and that adjacency invites reading the hero as a
  // filtered result — which it is not (see the independent-anchor note above; the
  // strip still labels its own month). Extra bottom space lets the sticky row own its
  // own boundary so the strip reads as a page header. The resolving fix — the strip
  // following the board's monthAnchor — needs this out of a Server Component and
  // reopens a locked decision, so it is deliberately NOT here.
  return (
    <section className="flex flex-wrap items-center gap-x-6 gap-y-2 border-b border-border pt-4 pb-7">
      <span
        className="text-[10.5px] font-medium uppercase text-muted-foreground"
        style={{ letterSpacing: "0.13em" }}
      >
        {/* "Spend" qualifier: the board can now show income rows, so the bare
            month label would let this spend-MTD number misread as income.
            "so far" qualifier: this window is month-to-date (currentMonthWindow
            ends at today), while the board's own stepper spans the FULL calendar
            month — the bare month name let the two disagree and read as a bug. */}
        Spend · {formatMonthYear(now)} so far
      </span>

      <div className="flex items-baseline gap-3.5">
        <span
          className={cn(
            "text-[44px] font-semibold leading-none tracking-[-0.024em] tabular-nums",
            currentQuery.isSuccess
              ? "text-foreground"
              : "text-muted-foreground",
          )}
          style={{ fontVariantNumeric: "tabular-nums lining-nums" }}
        >
          {currentQuery.isSuccess ? (
            <Sensitive>{formatINR(Math.abs(currentSpend))}</Sensitive>
          ) : (
            "—"
          )}
        </span>

        {showDelta ? (
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <span
                  tabIndex={0}
                  className="inline-flex cursor-help items-baseline gap-1 text-[12.5px] font-medium tabular-nums"
                  style={{ letterSpacing: "-0.003em" }}
                >
                  {direction === "down" || direction === "up" ? (
                    <span
                      className={deltaColor}
                      style={{ fontSize: 10, transform: "translateY(-1px)" }}
                    >
                      {direction === "down" ? "▼" : "▲"}
                    </span>
                  ) : null}
                  {deltaPct !== null ? (
                    <span className={deltaColor}>
                      {Math.abs(deltaPct).toFixed(1)}%
                    </span>
                  ) : null}
                  <span className="font-normal text-muted-foreground">
                    vs <Sensitive>{formatINRWhole(priorSpend)}</Sensitive>
                  </span>
                </span>
              </TooltipTrigger>
              <TooltipContent>{deltaWindowLabel}</TooltipContent>
            </Tooltip>
          </TooltipProvider>
        ) : null}
      </div>

      <div className="flex items-baseline gap-2">
        <span
          className="text-[10.5px] font-medium uppercase text-muted-foreground"
          style={{ letterSpacing: "0.13em" }}
        >
          Income · {now.toLocaleString("en-IN", { month: "long" })} so far
        </span>
        <span
          className={cn(
            "text-[20px] font-semibold leading-none tracking-[-0.02em] tabular-nums",
            incomeQuery.isSuccess ? "text-foreground" : "text-muted-foreground",
          )}
          style={{ fontVariantNumeric: "tabular-nums lining-nums" }}
        >
          {incomeQuery.isSuccess ? (
            <Sensitive>{formatINR(monthIncome)}</Sensitive>
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
  // Keep the box (no layout shift) while pending / empty.
  if (n === 0)
    return <svg width={w} height={h} aria-label="Last 14 weeks of spending" />;

  const gap = 1;
  const barW = (w - gap * (n - 1)) / n;
  const max = Math.max(1, ...data); // guard ÷0 when there's no spend

  return (
    <svg width={w} height={h} aria-label="Last 14 weeks of spending">
      {data.map((v, i) => {
        const bh = Math.max(2, (v / max) * (h - 2));
        const x = i * (barW + gap);
        const y = h - bh;
        const isLast = i === n - 1; // current (partial) week
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
