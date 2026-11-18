"use client";

/**
 * Spend Isolation & Card Usage Card (PRD §F3b, §F8, ADR-0017 Rule 7).
 *
 * Surfaces:
 * 1. Dedicated Metric Card for "Card Used by Friends this Month":
 *    Sum of credit card debits linked to peer_entries (direction = 'you_gave').
 * 2. Personal Spend Isolation Breakdown:
 *    Gross Spend vs. Fronted for Friends vs. Net Personal Spend
 *    (Net Personal Spend = Gross Spend - Fronted Peer Entries).
 *    Unpaired transfers remain excluded from personal spend.
 *
 * All financial amounts are wrapped with <Sensitive>.
 */

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";

import { getOverview, type OverviewResponse } from "@/lib/api/client";
import { formatINR } from "@/lib/format";
import { periodParams, type Period } from "@/lib/period";
import { Sensitive } from "@/components/balance-visibility";
import { Eyebrow } from "@/components/ui/eyebrow";
import { IconChevronRight, IconUsers } from "@/components/icons";

export type SpendIsolationCardProps = {
  overview?: OverviewResponse;
  period?: Period;
  ready?: boolean;
};

export function SpendIsolationCard({
  overview: externalOverview,
  period,
  ready: externalReady,
}: SpendIsolationCardProps) {
  // Current month in YYYY-MM format
  const now = new Date();
  const currentMonthKey = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;

  // If externalOverview isn't provided, fetch for the given period (or current month)
  const overviewQuery = useQuery({
    queryKey: [
      "dashboards",
      "overview",
      period ? periodParams(period) : { month: currentMonthKey },
    ],
    queryFn: () =>
      getOverview(period ? periodParams(period) : { month: currentMonthKey }),
    enabled: externalOverview === undefined,
  });

  // Always fetch current month overview specifically for the "Card Used by Friends this Month" tile
  // if the active view's period is not already the current month.
  const isCurrentMonthView =
    period?.mon !== undefined &&
    `${period.year}-${String(period.mon).padStart(2, "0")}` === currentMonthKey;

  const monthlyOverviewQuery = useQuery({
    queryKey: ["dashboards", "overview", { month: currentMonthKey }],
    queryFn: () => getOverview({ month: currentMonthKey }),
    enabled: !isCurrentMonthView,
  });

  const activeData = externalOverview ?? overviewQuery.data;
  const isReady =
    externalReady ?? (externalOverview !== undefined || overviewQuery.isSuccess);

  // Dedicated tile: Card Used by Friends this Month (always current month's card usage)
  const cardUsedThisMonthPaise = isCurrentMonthView
    ? (activeData?.fronted_card_usage_paise ?? 0)
    : (monthlyOverviewQuery.data?.fronted_card_usage_paise ??
      activeData?.fronted_card_usage_paise ??
      0);

  // Breakdown metrics for the active period (month or year)
  const grossSpendPaise = activeData?.gross_spend_paise ?? 0;
  const frontedPeerPaise = activeData?.fronted_peer_entries_paise ?? 0;
  const netPersonalSpendPaise = activeData?.net_personal_spend_paise ?? 0;

  return (
    <section className="rounded-lg border border-border bg-card">
      <div className="flex h-10 items-center justify-between border-b border-border/40 px-4">
        <div className="flex items-center gap-2">
          <IconUsers className="size-3.5 text-muted-foreground" />
          <Eyebrow as="h2">Friends & Spend Isolation</Eyebrow>
        </div>
        <Link
          href="/friends"
          className="flex items-center gap-1 text-[11px] font-medium text-muted-foreground transition-colors hover:text-foreground"
        >
          <span>Friends & splits</span>
          <IconChevronRight className="size-3" />
        </Link>
      </div>

      {!isReady ? (
        <p className="px-4 py-6 text-[13px] text-muted-foreground">Loading…</p>
      ) : (
        <div className="grid grid-cols-1 gap-4 p-4 sm:grid-cols-2 lg:grid-cols-[1.2fr_1.8fr]">
          {/* Tile 1: Card Used by Friends this Month */}
          <div className="flex flex-col justify-between rounded-md border border-border/50 bg-background/50 p-3.5">
            <div>
              <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground/80">
                Card Used by Friends this Month
              </span>
              <div className="mt-2 text-[26px] font-semibold leading-none tracking-[-0.02em] tabular-nums text-foreground">
                <Sensitive>{formatINR(cardUsedThisMonthPaise)}</Sensitive>
              </div>
            </div>
            <p className="mt-3 text-[11.5px] leading-relaxed text-muted-foreground">
              {cardUsedThisMonthPaise > 0
                ? "Credit card debits fronted for friends this month."
                : "No card debits fronted for friends this month."}
            </p>
          </div>

          {/* Tile 2: Personal Spend Isolation Breakdown */}
          <div className="flex flex-col justify-between rounded-md border border-border/50 bg-background/50 p-3.5">
            <div>
              <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground/80">
                Personal Spend Isolation
              </span>

              <div className="mt-2 grid grid-cols-3 gap-2 border-b border-border/30 pb-3">
                {/* Gross Spend */}
                <div className="flex flex-col">
                  <span className="text-[11px] text-muted-foreground">Gross Spend</span>
                  <span className="text-[14px] font-medium tabular-nums text-foreground">
                    <Sensitive>{formatINR(grossSpendPaise)}</Sensitive>
                  </span>
                </div>

                {/* Fronted for Friends */}
                <div className="flex flex-col">
                  <span className="text-[11px] text-muted-foreground">Fronted for Friends</span>
                  <span className="text-[14px] font-medium tabular-nums text-amber-600 dark:text-amber-500">
                    <Sensitive>
                      {frontedPeerPaise > 0 ? "−" : ""}
                      {formatINR(frontedPeerPaise)}
                    </Sensitive>
                  </span>
                </div>

                {/* Net Personal Spend */}
                <div className="flex flex-col">
                  <span className="text-[11px] font-medium text-foreground/80">Net Personal</span>
                  <span className="text-[15px] font-semibold tabular-nums text-foreground">
                    <Sensitive>{formatINR(netPersonalSpendPaise)}</Sensitive>
                  </span>
                </div>
              </div>
            </div>

            <p className="mt-2 text-[11px] text-muted-foreground/80">
              Net Personal Spend = Gross Spend − Fronted for Friends. Unpaired transfers stay excluded.
            </p>
          </div>
        </div>
      )}
    </section>
  );
}
