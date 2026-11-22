"use client";

/**
 * Tier 1: 5-Second Daily Financial Pulse Ribbon (PRD §F8 View 1 & Daily Operational Cockpit).
 *
 * Sits directly above the fold (~130px height) providing instant situational awareness:
 * 1. Total Net Worth (authoritative live scalar, 30d/1y deltas, asset/liability vitals)
 * 2. Liquid Working Capital (safe-to-spend bank+cash, runway buffer, account breakdown)
 * 3. Month Spend Pacing (current month spend, day-of-month progress e.g. Day 22 of 30, projected spend)
 * 4. Action & Attention Hub (pending statement reviews, Khata receivables/payables, quick navigation)
 */

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";

import { Sensitive } from "@/components/balance-visibility";
import { Eyebrow } from "@/components/ui/eyebrow";
import { MONO } from "@/components/ui/table";
import {
  getNetWorthSummary,
  listPendingImports,
  listPeriodTotals,
  type OverviewResponse,
} from "@/lib/api/client";
import { monthRange } from "@/lib/dates";
import { compactINR, formatINR, formatMonthYear } from "@/lib/format";
import { cn } from "@/lib/utils";

interface DailyPulseRibbonProps {
  overview?: OverviewResponse;
  isLoading?: boolean;
}

export function DailyPulseRibbon({ overview, isLoading }: DailyPulseRibbonProps) {
  const now = new Date();
  const currentDay = now.getDate();
  const daysInMonth = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
  const pctElapsed = Math.min(100, Math.max(1, Math.round((currentDay / daysInMonth) * 100)));

  // 1. Net Worth Summary (for 30d and 1y deltas)
  const summaryQuery = useQuery({
    queryKey: ["net-worth", "summary"],
    queryFn: () => getNetWorthSummary(),
  });
  const summary = summaryQuery.data;

  // 2. Pending Imports (for Action Hub)
  const pendingQuery = useQuery({
    queryKey: ["imports", "pending"],
    queryFn: listPendingImports,
  });
  const totalPending = (pendingQuery.data ?? []).reduce(
    (n, b) => n + b.pending_count,
    0,
  );

  // 3. Current Month Spending (for Month Spend Pacing)
  const { date_from: monthStart, date_to: monthEnd } = monthRange(
    new Date(now.getFullYear(), now.getMonth(), 1),
  );
  const monthTotalsQuery = useQuery({
    queryKey: ["dashboards", "period-totals", { start: monthStart, end: monthEnd }],
    queryFn: () => listPeriodTotals({ start: monthStart, end: monthEnd }),
  });

  const monthSpendPaise = Math.abs(monthTotalsQuery.data?.expense_paise ?? 0);
  const projectedSpendPaise =
    currentDay > 0
      ? Math.round((monthSpendPaise / currentDay) * daysInMonth)
      : monthSpendPaise;

  // Derive Liquid Cash (Bank + Cash accounts with positive balance)
  const accounts = overview?.accounts ?? [];
  const liquidAccounts = accounts.filter(
    (a) => (a.type === "bank" || a.type === "cash") && a.balance_paise > 0,
  );
  const liquidPaise = liquidAccounts.reduce((sum, a) => sum + a.balance_paise, 0);

  // Runway buffer in months based on current month spend rate (floor at 1000 INR to avoid /0)
  const monthlyBurn = Math.max(monthSpendPaise, 100000);
  const runwayMonths = (liquidPaise / monthlyBurn).toFixed(1);

  // Liquid accounts snippet (e.g. "HDFC ₹3.85L · Cash ₹11.6K")
  const topLiquidSnippets = liquidAccounts
    .slice(0, 2)
    .map((a) => `${a.name.split(" ")[0]} ${compactINR(a.balance_paise)}`)
    .join(" · ");

  // Net worth values
  const netWorthPaise = summary?.net_worth_paise ?? overview?.net_worth_paise ?? 0;
  const assetsPaise = summary?.assets_paise ?? 0;
  const liabilitiesPaise = summary?.liabilities_paise ?? 0;

  // Peer receivables and payables
  const peerReceivables = overview?.peer_receivables_paise ?? 0;
  const peerPayables = overview?.peer_payables_paise ?? 0;

  const delta30dPct = summary?.delta_30d_pct;
  const delta1yPct = summary?.delta_1y_pct;

  return (
    <section aria-label="Daily Financial Pulse" className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
      {/* ── Tile 1: Total Net Worth ── */}
      <div className="flex flex-col justify-between rounded-lg border border-border bg-card p-3.5 shadow-xs transition-shadow hover:shadow-sm">
        <div className="flex items-center justify-between">
          <Eyebrow as="h2">Total Net Worth</Eyebrow>
          <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-500/10 px-2 py-0.5 text-[10.5px] font-medium text-emerald-600 dark:text-emerald-400">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse" />
            Live
          </span>
        </div>

        <div className="my-2">
          <div
            className="text-[22px] font-semibold tracking-[-0.02em] text-foreground"
            style={MONO}
          >
            {isLoading && !overview ? (
              <span className="text-muted-foreground">—</span>
            ) : (
              <Sensitive>{formatINR(netWorthPaise)}</Sensitive>
            )}
          </div>
          <div className="mt-1 flex items-center gap-2 text-[11px]">
            {delta30dPct != null ? (
              <span
                className={cn(
                  "font-medium",
                  delta30dPct >= 0 ? "text-pos" : "text-neg",
                )}
              >
                {delta30dPct >= 0 ? "+" : ""}
                {delta30dPct.toFixed(1)}% (30d)
              </span>
            ) : null}
            {delta1yPct != null ? (
              <span className="text-muted-foreground/80">
                1y: {delta1yPct >= 0 ? "+" : ""}
                {delta1yPct.toFixed(1)}%
              </span>
            ) : null}
          </div>
        </div>

        <div className="border-t border-border/40 pt-2 text-[10.5px] text-muted-foreground">
          Assets <Sensitive>{compactINR(assetsPaise)}</Sensitive>
          {liabilitiesPaise > 0 ? (
            <>
              {" "}· Debt <Sensitive>{compactINR(liabilitiesPaise)}</Sensitive>
            </>
          ) : (
            " · No Debt"
          )}
        </div>
      </div>

      {/* ── Tile 2: Liquid Working Capital (Safe-to-Spend) ── */}
      <div className="flex flex-col justify-between rounded-lg border border-border bg-card p-3.5 shadow-xs transition-shadow hover:shadow-sm">
        <div className="flex items-center justify-between">
          <Eyebrow as="h2">Working Capital</Eyebrow>
          <span className="text-[10.5px] font-medium text-cyan-600 dark:text-cyan-400">
            Safe to spend
          </span>
        </div>

        <div className="my-2">
          <div
            className="text-[22px] font-semibold tracking-[-0.02em] text-cyan-600 dark:text-cyan-400"
            style={MONO}
          >
            {isLoading && !overview ? (
              <span className="text-muted-foreground">—</span>
            ) : (
              <Sensitive>{formatINR(liquidPaise)}</Sensitive>
            )}
          </div>
          <div className="mt-1 text-[11px] text-muted-foreground">
            <span className="font-medium text-foreground">{runwayMonths} Mo</span> Runway
            {topLiquidSnippets ? ` · ${topLiquidSnippets}` : ""}
          </div>
        </div>

        <div className="border-t border-border/40 pt-2 text-[10.5px] text-muted-foreground">
          {liquidAccounts.length} liquid account{liquidAccounts.length === 1 ? "" : "s"} ready
        </div>
      </div>

      {/* ── Tile 3: Month Spend Pacing Intelligence ── */}
      <div className="flex flex-col justify-between rounded-lg border border-border bg-card p-3.5 shadow-xs transition-shadow hover:shadow-sm">
        <div className="flex items-center justify-between">
          <Eyebrow as="h2">Spend · {formatMonthYear(now)}</Eyebrow>
          <span className="text-[10.5px] font-medium text-muted-foreground">
            Day {currentDay} of {daysInMonth}
          </span>
        </div>

        <div className="my-2">
          <div
            className="text-[22px] font-semibold tracking-[-0.02em] text-foreground"
            style={MONO}
          >
            {monthTotalsQuery.isLoading ? (
              <span className="text-muted-foreground">—</span>
            ) : (
              <Sensitive>{formatINR(monthSpendPaise)}</Sensitive>
            )}
          </div>
          <div className="mt-1 text-[11px] text-muted-foreground">
            {pctElapsed}% elapsed · Projected ~
            <Sensitive>{compactINR(projectedSpendPaise)}</Sensitive>
          </div>
        </div>

        <div className="border-t border-border/40 pt-2">
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
            <div
              className="h-full rounded-full bg-primary/70 transition-all duration-300"
              style={{ width: `${pctElapsed}%` }}
              title={`${pctElapsed}% of month elapsed`}
            />
          </div>
        </div>
      </div>

      {/* ── Tile 4: Action & Attention Hub ── */}
      <div className="flex flex-col justify-between rounded-lg border border-border bg-card p-3.5 shadow-xs transition-shadow hover:shadow-sm">
        <div className="flex items-center justify-between">
          <Eyebrow as="h2">Actions & Attention</Eyebrow>
          <span className="text-[10.5px] text-muted-foreground">Triage</span>
        </div>

        <div className="my-2 flex flex-col gap-1.5">
          {totalPending > 0 ? (
            <Link
              href="/imports/review"
              className="group flex items-center justify-between rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-[11.5px] font-medium text-amber-700 transition-colors hover:bg-amber-500/20 dark:text-amber-300"
            >
              <span className="flex items-center gap-1.5">
                <span className="h-1.5 w-1.5 rounded-full bg-amber-500 animate-pulse" />
                {totalPending} import{totalPending === 1 ? "" : "s"} to review
              </span>
              <span className="text-[11px] opacity-70 transition-transform group-hover:translate-x-0.5">
                ↗
              </span>
            </Link>
          ) : null}

          {peerReceivables > 0 ? (
            <Link
              href="/friends"
              className="group flex items-center justify-between rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2 py-1 text-[11.5px] font-medium text-emerald-700 transition-colors hover:bg-emerald-500/20 dark:text-emerald-300"
            >
              <span className="flex items-center gap-1.5">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
                Collect <Sensitive>{compactINR(peerReceivables)}</Sensitive>
              </span>
              <span className="text-[11px] opacity-70 transition-transform group-hover:translate-x-0.5">
                ↗
              </span>
            </Link>
          ) : null}

          {peerPayables > 0 && peerReceivables === 0 && totalPending === 0 ? (
            <Link
              href="/friends"
              className="group flex items-center justify-between rounded-md border border-rose-500/30 bg-rose-500/10 px-2 py-1 text-[11.5px] font-medium text-rose-700 transition-colors hover:bg-rose-500/20 dark:text-rose-300"
            >
              <span className="flex items-center gap-1.5">
                <span className="h-1.5 w-1.5 rounded-full bg-rose-500" />
                Pay <Sensitive>{compactINR(peerPayables)}</Sensitive>
              </span>
              <span className="text-[11px] opacity-70 transition-transform group-hover:translate-x-0.5">
                ↗
              </span>
            </Link>
          ) : null}

          {totalPending === 0 && peerReceivables === 0 && peerPayables === 0 ? (
            <div className="flex items-center gap-1.5 py-1 text-[11.5px] text-muted-foreground">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
              All caught up · zero pending items
            </div>
          ) : null}
        </div>

        <div className="flex items-center justify-between border-t border-border/40 pt-2 text-[10.5px]">
          <Link
            href="/expenses"
            className="font-medium text-primary hover:underline"
          >
            + Add transaction
          </Link>
          <Link
            href="/portfolio"
            className="text-muted-foreground hover:text-foreground"
          >
            Portfolio ↗
          </Link>
        </div>
      </div>
    </section>
  );
}
