"use client";

/**
 * Personal Spend Isolation Card for /spending (PRD §F3b, §F8, ADR-0017 Rule 7).
 *
 * Surfaces:
 * 1. Personal Spend Isolation Breakdown:
 *    Gross Spend vs. Fronted for Friends vs. Net Personal Spend
 *    (Net Personal Spend = Gross Spend - Fronted Peer Entries).
 *    Unpaired transfers remain excluded from personal spend.
 * 2. When fronted expenses exist, highlights the reimbursable portion.
 *    When 0, cleanly indicates 100% personal consumption without visual clutter.
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
  const now = new Date();
  const currentMonthKey = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;

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

  const activeData = externalOverview ?? overviewQuery.data;
  const isReady =
    externalReady ?? (externalOverview !== undefined || overviewQuery.isSuccess);

  const grossSpendPaise = activeData?.gross_spend_paise ?? 0;
  const frontedPeerPaise = activeData?.fronted_peer_entries_paise ?? 0;
  const netPersonalSpendPaise = activeData?.net_personal_spend_paise ?? 0;
  const hasFronted = frontedPeerPaise > 0;

  return (
    <section className="rounded-lg border border-border bg-card">
      <div className="flex h-10 items-center justify-between border-b border-border/40 px-4">
        <div className="flex items-center gap-2">
          <IconUsers className="size-3.5 text-muted-foreground" />
          <Eyebrow as="h2">Personal Spend Isolation</Eyebrow>
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
        <div className="p-4">
          <div className="rounded-md border border-border/50 bg-background/50 p-4">
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3 border-b border-border/30 pb-3.5">
              {/* Gross Spend */}
              <div className="flex flex-col">
                <span className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">
                  Gross Debit Spend
                </span>
                <span className="mt-1 text-[16px] font-semibold font-mono tabular-nums text-foreground">
                  <Sensitive>{formatINR(grossSpendPaise)}</Sensitive>
                </span>
                <span className="mt-0.5 text-[10.5px] text-muted-foreground">
                  Statement debits total
                </span>
              </div>

              {/* Fronted for Friends */}
              <div className="flex flex-col">
                <span className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">
                  Fronted for Friends
                </span>
                <span className="mt-1 text-[16px] font-semibold font-mono tabular-nums text-amber-600 dark:text-amber-500">
                  <Sensitive>
                    {hasFronted ? "−" : ""}
                    {formatINR(frontedPeerPaise)}
                  </Sensitive>
                </span>
                <span className="mt-0.5 text-[10.5px] text-muted-foreground">
                  {hasFronted
                    ? "Reimbursable peer splits"
                    : "No splits fronted for friends"}
                </span>
              </div>

              {/* Net Personal Spend */}
              <div className="flex flex-col">
                <span className="text-[11px] font-medium text-foreground/80 uppercase tracking-wider">
                  Net Personal Spend
                </span>
                <span className="mt-1 text-[18px] font-bold font-mono tabular-nums text-foreground">
                  <Sensitive>{formatINR(netPersonalSpendPaise)}</Sensitive>
                </span>
                <span className="mt-0.5 text-[10.5px] text-muted-foreground">
                  Your true personal burn
                </span>
              </div>
            </div>

            <p className="mt-2.5 text-[11px] text-muted-foreground">
              {hasFronted ? (
                <>
                  Net Personal Spend = Gross Spend − Fronted for Friends.{" "}
                  <Sensitive>{formatINR(frontedPeerPaise)}</Sensitive> in friend splits is tracked in Khata and excluded from your personal consumption.
                </>
              ) : (
                "100% of debit spending in this period represents personal consumption (no shared friend splits recorded)."
              )}
            </p>
          </div>
        </div>
      )}
    </section>
  );
}
