"use client";

/**
 * Friends & Dues Summary Card for Main Dashboard (PRD §F3b, §F8, ADR-0017).
 *
 * Designed for actionable financial clarity:
 * 1. Shows actionable pending dues ("To Collect" receivables / "To Pay" payables).
 * 2. If fronted spending exists in the period, shows a subtle Net Personal Spend note.
 * 3. ZERO-STATE AUTO-COLLAPSE: If there are no outstanding dues and no fronted spend,
 *    this component returns null so it never clutters the executive overview.
 *
 * All financial amounts are wrapped with <Sensitive>.
 */

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";

import { getPeerSummary, type OverviewResponse } from "@/lib/api/client";
import { formatINR } from "@/lib/format";
import { Sensitive } from "@/components/balance-visibility";
import { Eyebrow } from "@/components/ui/eyebrow";
import { IconChevronRight, IconUsers } from "@/components/icons";

export type FriendsDuesCardProps = {
  overview?: OverviewResponse;
};

export function FriendsDuesCard({ overview }: FriendsDuesCardProps) {
  const summaryQuery = useQuery({
    queryKey: ["friends", "summary"],
    queryFn: getPeerSummary,
  });

  const summary = summaryQuery.data;
  const receivablePaise = summary?.total_receivable_paise ?? 0;
  const payablePaise = summary?.total_payable_paise ?? 0;
  const frontedPeerPaise = overview?.fronted_peer_entries_paise ?? 0;
  const netPersonalSpendPaise = overview?.net_personal_spend_paise ?? 0;

  // Zero-State Auto-Collapse: If no dues and no fronted expenses in period,
  // do not occupy valuable executive dashboard space.
  const hasDues = receivablePaise > 0 || payablePaise > 0;
  const hasFronted = frontedPeerPaise > 0;

  if (!hasDues && !hasFronted) {
    return null;
  }

  return (
    <section className="rounded-lg border border-border bg-card">
      <div className="flex h-10 items-center justify-between border-b border-border/40 px-4">
        <div className="flex items-center gap-2">
          <IconUsers className="size-3.5 text-muted-foreground" />
          <Eyebrow as="h2">Friends & Khata Dues</Eyebrow>
        </div>
        <Link
          href="/friends"
          className="flex items-center gap-1 text-[11px] font-medium text-muted-foreground transition-colors hover:text-foreground"
        >
          <span>Friends & splits</span>
          <IconChevronRight className="size-3" />
        </Link>
      </div>

      <div className="p-4">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {/* Tile 1: To Collect (Receivable) */}
          <div className="flex flex-col justify-between rounded-md border border-border/50 bg-background/50 p-3.5">
            <div>
              <span className="text-[10px] font-medium uppercase tracking-wider text-emerald-600 dark:text-emerald-400">
                To Collect
              </span>
              <div className="mt-2 text-[22px] font-semibold leading-none tracking-[-0.02em] tabular-nums font-mono text-emerald-600 dark:text-emerald-400">
                <Sensitive>{formatINR(receivablePaise)}</Sensitive>
              </div>
            </div>
            <p className="mt-2 text-[11px] text-muted-foreground">
              {receivablePaise > 0
                ? "Total friends owe you across active contacts."
                : "No outstanding money owed to you."}
            </p>
          </div>

          {/* Tile 2: To Pay (Payable) */}
          <div className="flex flex-col justify-between rounded-md border border-border/50 bg-background/50 p-3.5">
            <div>
              <span className="text-[10px] font-medium uppercase tracking-wider text-amber-600 dark:text-amber-400">
                To Pay
              </span>
              <div className="mt-2 text-[22px] font-semibold leading-none tracking-[-0.02em] tabular-nums font-mono text-amber-600 dark:text-amber-400">
                <Sensitive>{formatINR(payablePaise)}</Sensitive>
              </div>
            </div>
            <p className="mt-2 text-[11px] text-muted-foreground">
              {payablePaise > 0
                ? "Total you owe friends across active contacts."
                : "You are fully settled up with friends."}
            </p>
          </div>

          {/* Tile 3: Net Personal Spend (if fronted spend exists) */}
          {hasFronted ? (
            <div className="flex flex-col justify-between rounded-md border border-border/50 bg-background/50 p-3.5 sm:col-span-2 lg:col-span-1">
              <div>
                <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground/80">
                  Net Personal Spend
                </span>
                <div className="mt-2 text-[22px] font-semibold leading-none tracking-[-0.02em] tabular-nums font-mono text-foreground">
                  <Sensitive>{formatINR(netPersonalSpendPaise)}</Sensitive>
                </div>
              </div>
              <p className="mt-2 text-[11px] text-muted-foreground">
                <Sensitive>{formatINR(frontedPeerPaise)}</Sensitive> fronted for friends deducted from gross spend.
              </p>
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}
