"use client";

/**
 * Financial Overview — the data-driven half of /dashboard (PRD §F8 view 1 + 4).
 *
 * Four bands, top to bottom:
 *   A — Net-worth hero + a quiet Assets · Investments · Owed decomposition (so
 *       the figure is never a black box). Derived from the overview aggregate.
 *       Two account types are excluded from net worth — credit cards (spend
 *       channels, not liabilities) and investment accounts (placeholders; their
 *       value is already the "Investments" term) — so "Owed" reflects only
 *       genuine bank/cash debt (e.g. an overdraft) and is usually absent. The
 *       exclusion set mirrors the server's; see NET_WORTH_EXCLUDED_TYPES below.
 *   B — "This month": the shipped SummaryStrip (spend hero + MoM delta + income
 *       + 13-week sparkline). The only period-scoped band; A/C/D are all-time /
 *       snapshot.
 *   C — Accounts (grouped by type, signed balances; credit cards show
 *       year-to-date spend instead of a balance) + Portfolio value (with a
 *       holding count and, when they apply, the two exclusion notes: holdings
 *       with no NAV, and priced holdings with no cached FX rate).
 *   D — Recent activity: the 6 newest confirmed transactions.
 *
 * Rendered in our own flat surfaces (rounded-lg border bg-card) — color carries
 * meaning (sign), never decoration. The net-worth hero matches SummaryStrip's
 * Hanken-tabular hero; smaller amounts use the shared MONO (JetBrains Mono)
 * recipe like the boards. Every amount is wrapped in <Sensitive>.
 *
 * Query keys are `["dashboards", …]` / `["transactions", …]` prefixed so the
 * existing transaction/account mutations (which invalidate those prefixes)
 * refresh the bands without a reload (PRD §F9).
 */
import type { ReactNode } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";

import {
  getOverview,
  listCategories,
  listHoldings,
  listTransactions,
  type AccountBalanceRow,
  type CategoryColor,
  type OverviewResponse,
  type TransactionRead,
} from "@/lib/api/client";
import { formatDate, formatINR } from "@/lib/format";
import { cn } from "@/lib/utils";
import { CategoryDot } from "@/components/category-dot";
import { Sensitive } from "@/components/balance-visibility";
import { MONO } from "@/components/ui/table";
import { Eyebrow } from "@/components/ui/eyebrow";
import { SummaryStrip } from "@/components/dashboard/summary-strip";

// Hand-mirror of the backend's NET_WORTH_EXCLUDED_TYPES
// (app/schemas/dashboards.py), which is the source of truth. tsc cannot catch
// drift here — see frontend/CLAUDE.md §"The tsc blind spot" — so keep it in sync
// by hand, as accounts-manager.tsx already does for SUPPORTED_CC_ISSUERS. Credit
// cards are spend channels, not liabilities; investment accounts are placeholders
// whose value is already counted in portfolio_value_paise. The accounts panel
// below still lists both — only the net-worth arithmetic skips them.
const NET_WORTH_EXCLUDED_TYPES: ReadonlySet<AccountBalanceRow["type"]> = new Set([
  "credit_card",
  "investment",
]);

// Display order for the accounts panel — liquid first, then cards, then the
// investment-account bucket (its holdings value lives in the Portfolio card).
const ACCOUNT_GROUPS: { type: AccountBalanceRow["type"]; label: string }[] = [
  { type: "bank", label: "Bank" },
  { type: "cash", label: "Cash" },
  { type: "credit_card", label: "Credit cards" },
  { type: "investment", label: "Investment accounts" },
];

export function Overview() {
  // Current calendar month (YYYY-MM), local — drives the month-scoped figures.
  const now = new Date();
  const month = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;

  const overviewQuery = useQuery({
    queryKey: ["dashboards", "overview", { month }],
    queryFn: () => getOverview({ month }),
  });
  // Recent activity — own cache entry, still `["transactions"]`-prefixed so txn
  // mutations invalidate it. The board's confirmed-only list is newest-first.
  const recentQuery = useQuery({
    queryKey: ["transactions", { recent: 6 }],
    queryFn: () => listTransactions({ limit: 6 }),
  });
  // Holdings drive the Portfolio card's count + the two exclusion notes (no NAV /
  // no FX rate); the value itself comes from the overview aggregate so it always
  // matches net worth. Shares the ["holdings"] cache with /holdings.
  const holdingsQuery = useQuery({
    queryKey: ["holdings"],
    queryFn: listHoldings,
  });
  // Shared ["categories"] cache — recent-activity dots use the same picked color
  // as the expenses board (else the same category would show two colors).
  const categoriesQuery = useQuery({
    queryKey: ["categories"],
    queryFn: listCategories,
  });

  const data = overviewQuery.data;
  const accountsById = new Map<number, string>(
    (data?.accounts ?? []).map((a) => [a.account_id, a.name]),
  );
  const categoryColorById = new Map<number, CategoryColor | null>(
    (categoriesQuery.data ?? []).map((c) => [c.id, c.color]),
  );
  const holdings = holdingsQuery.data?.holdings ?? [];
  // "Unpriced" = no NAV at all. Kept separate from the FX case below, which DOES
  // have a NAV and only lacks a conversion rate — calling those "unpriced" would
  // be wrong.
  const nullNav = holdings.filter(
    (h) => h.current_value_native_paise == null,
  ).length;
  // Priced, but excluded from any INR rollup for want of a cached USD→INR rate.
  // Derived from the same holdings list as `nullNav` (not from overview's
  // fx_unavailable_count) so the two counts can't disagree across a snapshot
  // boundary — the two figures come from different endpoints.
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
    <div className="flex flex-col gap-5">
      {/* The page's single h1 — the net-worth figure is the visual headline, so
          the heading is visually hidden but present for the document outline. */}
      <h1 className="sr-only">Overview</h1>
      <NetWorthHero data={data} />
      <SummaryStrip />
      <div className="grid gap-4 lg:grid-cols-[1.6fr_1fr]">
        <AccountsCard
          accounts={data?.accounts ?? []}
          ready={data !== undefined}
        />
        <PortfolioCard
          valuePaise={data?.portfolio_value_paise ?? 0}
          // Priced (NAV-bearing) count, matching /portfolio's holdings_count, so
          // one null-NAV holding doesn't read as "1 holding" here and "0 holdings"
          // there. holdings_count's server-side test is
          // `current_value_inr_paise is not None`, so BOTH exclusions must come
          // off — subtracting only nullNav made the two pages disagree by exactly
          // fxUnavailable. Each remainder gets its own footnote.
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
      <RecentActivity
        rows={recentQuery.data ?? []}
        accountsById={accountsById}
        categoryColorById={categoryColorById}
        status={recentQuery.status}
      />
    </div>
  );
}

function NetWorthHero({ data }: { data: OverviewResponse | undefined }) {
  const accounts = data?.accounts ?? [];
  const netWorth = data?.net_worth_paise ?? 0;
  const investments = data?.portfolio_value_paise ?? 0;
  // Both `assets` and `owed` skip every excluded type, mirroring the server's
  // net_worth_paise so that assets + investments − owed reconciles exactly with
  // it and the client can't drift. Filtering on credit_card alone was the drift:
  // an investment account created before its balance was pinned to 0 has no
  // correction path, so its stored balance would land in `assets` while the
  // headline (correctly) leaves it out. assets = Σ max(0, balance) of
  // contributing accounts; owed = Σ max(0, −balance) of the same (normally 0 —
  // only a genuine bank overdraft surfaces an "Owed" line).
  const contributing = accounts.filter(
    (a) => !NET_WORTH_EXCLUDED_TYPES.has(a.type),
  );
  const assets = contributing.reduce(
    (s, a) => s + Math.max(0, a.balance_paise),
    0,
  );
  const owed = contributing.reduce(
    (s, a) => s + Math.max(0, -a.balance_paise),
    0,
  );

  return (
    <section>
      <Eyebrow as="h2">Net worth</Eyebrow>
      <div
        className="mt-1.5 text-[44px] font-semibold leading-none tracking-[-0.024em] tabular-nums text-foreground"
        style={{ fontVariantNumeric: "tabular-nums lining-nums" }}
      >
        {data ? (
          <Sensitive>
            {netWorth < 0 ? "−" : ""}
            {formatINR(Math.abs(netWorth))}
          </Sensitive>
        ) : (
          <span className="text-muted-foreground">—</span>
        )}
      </div>
      {data ? (
        <p
          className="mt-2.5 text-[12px] text-muted-foreground"
          style={{ letterSpacing: "-0.003em" }}
        >
          Assets <Sensitive>{formatINR(assets)}</Sensitive>
          {" · "}
          Investments <Sensitive>{formatINR(investments)}</Sensitive>
          {owed > 0 ? (
            <>
              {" · "}
              Owed <Sensitive>−{formatINR(owed)}</Sensitive>
            </>
          ) : null}
        </p>
      ) : null}
      {/* net_worth_paise is short by any priced-but-unconvertible USD holding, so
          say so rather than render an understated headline silently. Read off the
          same response as the figure it qualifies. */}
      {data && data.fx_unavailable_count > 0 ? (
        <p className="mt-1.5 text-[11.5px] text-muted-foreground">
          ⚠ Excludes {data.fx_unavailable_count} USD holding
          {data.fx_unavailable_count === 1 ? "" : "s"} — no USD→INR rate is
          cached, so {data.fx_unavailable_count === 1 ? "its" : "their"} value
          isn’t counted here.
        </p>
      ) : null}
    </section>
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
}: {
  accounts: AccountBalanceRow[];
  ready: boolean;
}) {
  return (
    <section className="rounded-lg border border-border bg-card">
      <div className="flex h-10 items-center px-4">
        <Eyebrow as="h2">Accounts</Eyebrow>
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
                  {/* Credit cards show year-to-date spend, not a balance owed
                      (they're spend channels — bill payments aren't recorded).
                      The caption makes the number's meaning explicit, and names
                      the exclusion for the same reason the investment one does:
                      credit_card is in the server's NET_WORTH_EXCLUDED_TYPES,
                      so this group contributes nothing to the headline above. */}
                  {g.type === "credit_card" ? (
                    <span className="text-[10px] text-muted-foreground/70">
                      spent this year · not in net worth
                    </span>
                  ) : null}
                  {/* Same idea, other excluded type: an investment account is a
                      placeholder, so its balance is out of the headline above.
                      New ones are always ₹0, but a row created before that rule
                      keeps its balance and has no correction path — without this
                      caption it reads as money the hero forgot. */}
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
  // Credit cards render year-to-date spend (a neutral, non-negative magnitude),
  // never a red "owed" balance — they're spend channels, not liabilities. The
  // server sends spend_ytd_paise as a signed net (spend + refund); floor via
  // max(0, −signed) — NOT Math.abs — so a refund-dominant / net-credit window
  // (positive signed sum) shows ₹0 spent rather than a false positive spend.
  const isCard = account.type === "credit_card";
  const displayPaise = isCard
    ? Math.max(0, -(account.spend_ytd_paise ?? 0))
    : Math.abs(account.balance_paise);
  const neg = !isCard && account.balance_paise < 0;
  return (
    <div className="flex items-center justify-between gap-3 px-4 py-1.5">
      <span className="min-w-0 truncate text-[12.5px] text-foreground/90">
        {account.name}
        {/* The leading {" "} is load-bearing, not formatting: accessible-name
            computation concatenates adjacent inline children without inserting
            a separator, so without it the row announces "Axis CC Aarchived".
            Same fix, same shape as review-queue.tsx:868-871. */}
        {account.archived ? (
          <span className="ml-1.5 text-[10.5px] text-muted-foreground/70">
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
      <div className="flex h-10 items-center px-4">
        <Eyebrow as="h2">Portfolio</Eyebrow>
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
        <p className="mt-2.5 text-[11.5px] text-muted-foreground">
          {count !== null
            ? `${count} holding${count === 1 ? "" : "s"}`
            : "Current value"}
          {nullNav > 0 ? ` · ${nullNav} unpriced (₹0)` : ""}
          {fxUnavailable > 0 ? ` · ${fxUnavailable} excluded (no FX rate)` : ""}
        </p>
        <Link
          href="/holdings"
          className="mt-3 inline-block text-[12px] font-medium text-primary hover:underline"
        >
          View holdings →
        </Link>
      </div>
    </section>
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
  return (
    <section className="rounded-lg border border-border bg-card">
      <div className="flex h-10 items-center justify-between px-4">
        <Eyebrow as="h2">Recent activity</Eyebrow>
        <Link
          href="/expenses"
          className="text-[12px] font-medium text-primary hover:underline"
        >
          View all →
        </Link>
      </div>
      {status === "pending" ? (
        <p className="px-4 pb-4 text-[13px] text-muted-foreground">Loading…</p>
      ) : status === "error" ? (
        <p className="px-4 pb-4 text-[13px] text-neg">
          Couldn’t load recent activity.
        </p>
      ) : rows.length === 0 ? (
        <p className="px-4 pb-4 text-[13px] text-muted-foreground">
          Nothing yet — import a statement or add a transaction.
        </p>
      ) : (
        <div className="pb-1">
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
  // Sign is the source of truth (PRD §F4a): positive = credit (green, "+"),
  // negative = spend magnitude.
  const isCredit = txn.amount_paise > 0;
  const merchant = txn.merchant_raw?.trim() || "—";
  return (
    <div className="flex items-center gap-3 border-t border-border/60 px-4 py-2">
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
