"use client";

/**
 * Financial Overview — The 3-Tier Daily Operational Cockpit (PRD §F8 Views 1 & 4).
 *
 * Three hierarchical screen tiers designed for daily user efficiency:
 *   Tier 1 — 5-Second Daily Financial Pulse Ribbon (~130px · Above the Fold):
 *            4 glanceable operational tiles:
 *            1. Total Net Worth (authoritative live scalar, 30d/1y deltas, vitals)
 *            2. Liquid Working Capital (safe-to-spend bank+cash, runway buffer)
 *            3. Month Spend Pacing (day-of-month progress e.g. Day 22 of 30, pacing)
 *            4. Action & Attention Hub (pending statement reviews, Khata dues, quick links)
 *
 *   Tier 2 — Two-Column Operational Desk (~480px · Primary Screenfold):
 *            Left (1.4fr): Cashflow Velocity & Activity Ledger:
 *                          - Month Inflow/Outflow/Net cashflow summary
 *                          - 14-week trailing spend velocity sparkline
 *                          - Stream of 8 newest confirmed transactions with category dots
 *            Right (1fr):  Liquidity Accounts, Credit Cards, Khata Dues & Portfolio:
 *                          - Grouped bank, cash, credit cards (with YTD spend breakdown)
 *                          - Friends & Khata dues (To Collect vs To Pay)
 *                          - Portfolio valuation with live NAV indicator & /portfolio link
 *
 *   Tier 3 — Strategic Wealth Horizon Deck (~380px · Tabbed Deck):
 *            Consolidates low-frequency macro visualizers into a Radix Tabbed Deck:
 *            - Tab 1: Net Worth Trajectory (7-asset stacked area chart, milestones, 1M-ALL)
 *            - Tab 2: Cashflow Waterfall (Annual Inflow-to-Wealth conversion CY/FY)
 *
 * Rendered in flat, theme-aware surfaces (rounded-lg border bg-card).
 * All monetary amounts are wrapped in <Sensitive> for privacy masking.
 */
import { useState, type ReactNode } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";

import {
  getOverview,
  listCategories,
  listHoldings,
  listPeriodTotals,
  listSpendByPeriod,
  listTransactions,
  type AccountBalanceRow,
  type CategoryColor,
  type OverviewResponse,
  type TransactionRead,
} from "@/lib/api/client";
import { formatDate, formatINR, formatMonthYear } from "@/lib/format";
import { currentYear, periodLabel, periodParams, type Period } from "@/lib/period";
import { monthRange, trailingWeeksWindow } from "@/lib/dates";
import { cn } from "@/lib/utils";
import { CategoryDot } from "@/components/category-dot";
import { Sensitive, useBalanceHidden } from "@/components/balance-visibility";
import { MONO } from "@/components/ui/table";
import { Eyebrow } from "@/components/ui/eyebrow";
import { DailyPulseRibbon } from "@/components/dashboard/daily-pulse-ribbon";
import { FriendsDuesCard } from "@/components/dashboard/friends-dues-card";
import { MacroWealthDeck } from "@/components/dashboard/macro-wealth-deck";
import { PeriodPicker } from "@/components/dashboard/period-picker";
import { useAvailableYears } from "@/components/dashboard/use-available-years";

// Hand-mirror of the backend's NET_WORTH_EXCLUDED_TYPES (app/schemas/dashboards.py)
const NET_WORTH_EXCLUDED_TYPES: ReadonlySet<AccountBalanceRow["type"]> = new Set([
  "credit_card",
  "investment",
]);

// Display order for the accounts panel — liquid first, then cards, then investments
const ACCOUNT_GROUPS: { type: AccountBalanceRow["type"]; label: string }[] = [
  { type: "bank", label: "Bank" },
  { type: "cash", label: "Cash" },
  { type: "credit_card", label: "Credit cards" },
  { type: "investment", label: "Investment accounts" },
];

export function Overview() {
  const yearsQuery = useAvailableYears();
  const [pickedYear, setPickedYear] = useState<number | null>(null);
  const year = pickedYear ?? yearsQuery.years[0] ?? currentYear();
  const period: Period = { year };

  const overviewQuery = useQuery({
    queryKey: ["dashboards", "overview", periodParams(period)],
    queryFn: () => getOverview(periodParams(period)),
  });

  // Recent activity — 8 newest confirmed transactions
  const recentQuery = useQuery({
    queryKey: ["transactions", { recent: 8 }],
    queryFn: () => listTransactions({ limit: 8 }),
  });

  // Holdings drive the Portfolio card's count + exclusion notes
  const holdingsQuery = useQuery({
    queryKey: ["holdings"],
    queryFn: listHoldings,
  });

  // Shared categories cache for category dot colours
  const categoriesQuery = useQuery({
    queryKey: ["categories"],
    queryFn: () => listCategories(),
  });

  const data = overviewQuery.data;
  const accountsById = new Map<number, string>(
    (data?.accounts ?? []).map((a) => [a.account_id, a.name]),
  );

  const allCategories = categoriesQuery.data ?? [];
  const categoryColorById = new Map<number, CategoryColor | null>(
    allCategories.map((c) => [c.id, c.display_color]),
  );

  const holdings = holdingsQuery.data?.holdings ?? [];
  const nullNav = holdings.filter(
    (h) => h.current_value_native_paise == null,
  ).length;
  const fxUnavailable = holdings.filter(
    (h) =>
      h.current_value_native_paise != null && h.current_value_inr_paise == null,
  ).length;

  if (overviewQuery.status === "error") {
    return (
      <p className="py-16 text-center text-[13px] text-neg">
        Couldn’t load your overview — is the API running?
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <h1 className="sr-only">Overview</h1>

      {/* Tier 1: 5-Second Daily Financial Pulse Ribbon */}
      <DailyPulseRibbon overview={data} isLoading={overviewQuery.isLoading} />

      {/* Tier 2: Two-Column Operational Desk */}
      <div className="grid gap-5 lg:grid-cols-[1.4fr_1fr] items-start">
        {/* Left Column (1.4fr): Cashflow Velocity & Activity Ledger */}
        <RecentActivity
          rows={recentQuery.data ?? []}
          accountsById={accountsById}
          categoryColorById={categoryColorById}
          status={recentQuery.status}
        />

        {/* Right Column (1fr): Liquidity Accounts, Credit Cards, Khata Dues & Portfolio */}
        <div className="flex flex-col gap-4">
          <AccountsCard
            accounts={data?.accounts ?? []}
            ready={data !== undefined}
            period={period}
            availableYears={yearsQuery.years}
            onPeriodChange={(p) => setPickedYear(p.year)}
          />
          <FriendsDuesCard overview={data} />
          <PortfolioCard
            valuePaise={data?.portfolio_value_paise ?? 0}
            count={
              holdingsQuery.isSuccess
                ? holdings.length - nullNav - fxUnavailable
                : null
            }
            nullNav={nullNav}
            fxUnavailable={fxUnavailable}
            ready={data !== undefined}
          />
        </div>
      </div>

      {/* Tier 3: Macro Wealth Horizon Deck (Radix Tabbed) */}
      <MacroWealthDeck overview={data} />
    </div>
  );
}

function GroupLabel({ children }: { children: ReactNode }) {
  return (
    <span
      className="text-[10px] font-medium uppercase text-muted-foreground/80"
      style={{ letterSpacing: "0.11em" }}
    >
      {children}
    </span>
  );
}

function AccountsCard({
  accounts,
  ready,
  period,
  availableYears,
  onPeriodChange,
}: {
  accounts: AccountBalanceRow[];
  ready: boolean;
  period: Period;
  availableYears: number[];
  onPeriodChange: (p: Period) => void;
}) {
  const year = period.year;
  return (
    <section className="rounded-lg border border-border bg-card">
      <div className="flex h-10 items-center justify-between px-4">
        <Eyebrow as="h2">Accounts</Eyebrow>
        <PeriodPicker
          period={period}
          availableYears={availableYears}
          onChange={onPeriodChange}
          allowMonth={false}
        />
      </div>
      {!ready ? (
        <p className="px-4 pb-4 text-[13px] text-muted-foreground">Loading…</p>
      ) : accounts.length === 0 ? (
        <p className="px-4 pb-4 text-[13px] text-muted-foreground">
          No accounts yet — add one in Settings.
        </p>
      ) : (
        <div className="pb-2">
          {ACCOUNT_GROUPS.map((g) => {
            const rows = accounts.filter((a) => a.type === g.type);
            if (rows.length === 0) return null;
            return (
              <div key={g.type}>
                <div className="flex items-baseline gap-2 px-4 pb-1 pt-2.5">
                  <GroupLabel>{g.label}</GroupLabel>
                  {g.type === "credit_card" ? (
                    <span className="text-[10px] text-muted-foreground/70">
                      spent in {periodLabel({ year })} · not in net worth
                    </span>
                  ) : null}
                  {g.type === "investment" ? (
                    <span className="text-[10px] text-muted-foreground/70">
                      not in net worth
                    </span>
                  ) : null}
                </div>
                {rows.map((a) => (
                  <AccountRow key={a.account_id} account={a} />
                ))}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

function AccountRow({ account }: { account: AccountBalanceRow }) {
  const isCard = account.type === "credit_card";
  const displayPaise = isCard
    ? Math.max(0, -(account.spend_ytd_paise ?? 0))
    : Math.abs(account.balance_paise);
  const neg = !isCard && account.balance_paise < 0;

  const grossSpendPaise = isCard ? Math.abs(account.gross_spend_ytd_paise ?? 0) : 0;
  const refundPaise = isCard ? (account.refund_ytd_paise ?? 0) : 0;
  const cashbackPaise = isCard ? (account.cashback_ytd_paise ?? 0) : 0;
  const hasBreakdown =
    isCard &&
    (refundPaise > 0 ||
      cashbackPaise > 0 ||
      (grossSpendPaise > 0 && grossSpendPaise !== displayPaise));

  return (
    <div className="flex flex-col border-b border-border/30 last:border-0 px-4 py-2">
      <div className="flex items-center justify-between gap-3">
        <span className="min-w-0 truncate text-[12.5px] font-medium text-foreground/90">
          {account.name}
          {account.archived ? (
            <span className="ml-1.5 text-[10.5px] font-normal text-muted-foreground/70">
              {" "}
              archived
            </span>
          ) : null}
        </span>
        <span
          className={cn("text-[12.5px]", neg ? "text-neg" : "text-foreground")}
          style={MONO}
        >
          <Sensitive>
            {neg ? "−" : ""}
            {formatINR(displayPaise)}
          </Sensitive>
        </span>
      </div>

      {hasBreakdown ? (
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground/80">
          {grossSpendPaise > 0 ? (
            <span>
              Spend: <Sensitive>{formatINR(grossSpendPaise)}</Sensitive>
            </span>
          ) : null}
          {refundPaise > 0 ? (
            <span className="text-pos">
              Refund: <Sensitive>+{formatINR(refundPaise)}</Sensitive>
            </span>
          ) : null}
          {cashbackPaise > 0 ? (
            <span className="text-pos">
              Cashback: <Sensitive>+{formatINR(cashbackPaise)}</Sensitive>
            </span>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function PortfolioCard({
  valuePaise,
  count,
  nullNav,
  fxUnavailable,
  ready,
}: {
  valuePaise: number;
  count: number | null;
  nullNav: number;
  fxUnavailable: number;
  ready: boolean;
}) {
  return (
    <section className="rounded-lg border border-border bg-card">
      <div className="flex h-10 items-center justify-between px-4">
        <Eyebrow as="h2">Portfolio</Eyebrow>
        <span className="text-[10.5px] font-medium text-emerald-600 dark:text-emerald-400">
          • AMFI NAV Live
        </span>
      </div>
      <div className="px-4 pb-4">
        <div
          className="text-[20px] font-semibold leading-none tracking-[-0.02em] text-foreground"
          style={MONO}
        >
          {ready ? (
            <Sensitive>{formatINR(valuePaise)}</Sensitive>
          ) : (
            <span className="text-muted-foreground">—</span>
          )}
        </div>
        <p className="mt-2 text-[11.5px] text-muted-foreground">
          {count !== null
            ? `${count} holding${count === 1 ? "" : "s"}`
            : "Current value"}
          {nullNav > 0 ? ` · ${nullNav} unpriced (₹0)` : ""}
          {fxUnavailable > 0 ? ` · ${fxUnavailable} excluded (no FX rate)` : ""}
        </p>
        <Link
          href="/portfolio"
          className="mt-3 inline-flex items-center gap-1 text-[12px] font-medium text-primary hover:underline"
        >
          <span>View portfolio dashboard</span>
          <span>→</span>
        </Link>
      </div>
    </section>
  );
}

function Sparkline({ data }: { data: number[] }) {
  const w = 72;
  const h = 22;
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

function RecentActivity({
  rows,
  accountsById,
  categoryColorById,
  status,
}: {
  rows: TransactionRead[];
  accountsById: Map<number, string>;
  categoryColorById: Map<number, CategoryColor | null>;
  status: "pending" | "error" | "success";
}) {
  const now = new Date();
  const weekly = trailingWeeksWindow(now, 13);
  const { hidden } = useBalanceHidden();

  // 14-week trailing spend velocity
  const weeklyQuery = useQuery({
    queryKey: ["dashboards", "spend-by-period", { bucket: "week", ...weekly }],
    queryFn: () => listSpendByPeriod({ bucket: "week", ...weekly }),
  });
  const sparkData = (weeklyQuery.data?.buckets ?? []).map((b) =>
    Math.max(0, -b.total_paise),
  );

  // Current month cashflow
  const { date_from: monthStart, date_to: monthEnd } = monthRange(
    new Date(now.getFullYear(), now.getMonth(), 1),
  );
  const monthTotalsQ = useQuery({
    queryKey: [
      "dashboards",
      "period-totals",
      { start: monthStart, end: monthEnd },
    ],
    queryFn: () => listPeriodTotals({ start: monthStart, end: monthEnd }),
  });

  const monthSpend = Math.abs(monthTotalsQ.data?.expense_paise ?? 0);
  const monthIncome = monthTotalsQ.data?.income_paise ?? 0;
  const netCashflow = monthIncome - monthSpend;

  return (
    <section className="rounded-lg border border-border bg-card">
      {/* Header with Title, Sparkline and View All */}
      <div className="flex h-11 items-center justify-between border-b border-border/40 px-4">
        <Eyebrow as="h2">Recent activity & Cashflow</Eyebrow>
        <div className="flex items-center gap-3">
          <div
            className={cn("hidden sm:flex items-center gap-1.5", hidden && "select-none blur-[3px]")}
            title="14-week spending velocity"
          >
            <span className="text-[10px] text-muted-foreground/70">14w velocity</span>
            <Sparkline data={sparkData} />
          </div>
          <Link
            href="/expenses"
            className="text-[12px] font-medium text-primary hover:underline"
          >
            View all →
          </Link>
        </div>
      </div>

      {/* Cashflow Velocity Bar */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/30 bg-muted/20 px-4 py-2 text-[11.5px]">
        <div className="flex items-center gap-2.5">
          <span className="text-muted-foreground">{formatMonthYear(now)}:</span>
          <span className="text-pos">
            Inflow <Sensitive>+{formatINR(monthIncome)}</Sensitive>
          </span>
          <span className="text-muted-foreground/60">·</span>
          <span className="text-foreground">
            Outflow <Sensitive>−{formatINR(monthSpend)}</Sensitive>
          </span>
        </div>
        <div>
          <span className="text-muted-foreground">Net </span>
          <span
            className={cn(
              "font-semibold",
              netCashflow >= 0 ? "text-pos" : "text-neg",
            )}
          >
            <Sensitive>
              {netCashflow >= 0 ? "+" : "−"}
              {formatINR(Math.abs(netCashflow))}
            </Sensitive>
          </span>
        </div>
      </div>

      {/* Transactions List */}
      {status === "pending" ? (
        <p className="px-4 py-6 text-[13px] text-muted-foreground">Loading transactions…</p>
      ) : status === "error" ? (
        <p className="px-4 py-6 text-[13px] text-neg">
          Couldn’t load recent activity.
        </p>
      ) : rows.length === 0 ? (
        <p className="px-4 py-6 text-[13px] text-muted-foreground">
          Nothing yet — import a statement or add a transaction.
        </p>
      ) : (
        <div className="divide-y divide-border/40">
          {rows.map((t) => (
            <ActivityRow
              key={t.id}
              txn={t}
              accountName={accountsById.get(t.account_id) ?? "—"}
              categoryColor={
                t.category_id != null
                  ? (categoryColorById.get(t.category_id) ?? null)
                  : null
              }
            />
          ))}
        </div>
      )}

      {/* Footer Quick Action */}
      <div className="flex items-center justify-between border-t border-border/40 bg-muted/10 px-4 py-2 text-[11px]">
        <Link
          href="/expenses"
          className="font-medium text-primary hover:underline"
        >
          + Add transaction
        </Link>
        <span className="text-muted-foreground/70">
          Showing {rows.length} newest confirmed
        </span>
      </div>
    </section>
  );
}

function ActivityRow({
  txn,
  accountName,
  categoryColor,
}: {
  txn: TransactionRead;
  accountName: string;
  categoryColor: CategoryColor | null;
}) {
  const isCredit = txn.amount_paise > 0;
  const merchant = txn.merchant_raw?.trim() || "—";
  return (
    <div className="flex items-center gap-3 px-4 py-2 transition-colors hover:bg-muted/15">
      <span
        className="w-[72px] shrink-0 text-[11.5px] tabular-nums text-muted-foreground"
        style={{ fontVariantNumeric: "tabular-nums lining-nums" }}
      >
        {formatDate(txn.date)}
      </span>
      <CategoryDot categoryId={txn.category_id} color={categoryColor} />
      <span
        className="min-w-0 flex-1 truncate text-[12.5px] text-foreground/90"
        title={merchant === "—" ? undefined : merchant}
      >
        {merchant}
      </span>
      <span className="hidden shrink-0 truncate text-[11.5px] text-muted-foreground sm:block">
        {accountName}
      </span>
      <span
        className={cn(
          "shrink-0 text-[12.5px] font-medium",
          isCredit ? "text-pos" : "text-foreground",
        )}
        style={MONO}
      >
        <Sensitive>
          {isCredit ? "+" : ""}
          {formatINR(Math.abs(txn.amount_paise))}
        </Sensitive>
      </span>
    </div>
  );
}
