"use client";

/**
 * Portfolio-vs-benchmark performance (PRD §F8 view 5) — the scalar "am I beating
 * the market" answer, from GET /portfolio/performance with a benchmark picker.
 *
 * Four tiles: your XIRR, the benchmark fund's XIRR, the alpha (difference in % points,
 * green = you're ahead), and the rupee comparison ("the index would be ₹X"). XIRR / alpha
 * are ratios → never masked; only the ₹ figures wrap <Sensitive>. The honesty notes below
 * the tiles are PRD-required, not decoration: the benchmark is a post-expense *fund* (not
 * the raw index); `partial` (history gap), `benchmark_cache_stale` (refresh needed),
 * `is_multi_asset` (rough yardstick), a stale-valuation warning, and the two FX caveats
 * (`fx_staleness_days` = the USD→INR rate used is old; `fx_unavailable_count` = priced USD
 * holdings left out entirely) each surface a caveat rather than letting a wrong number
 * stand. When the benchmark return can't be solved we show "—" with the reason, never a
 * fabricated figure.
 */
import { type CSSProperties, type ReactNode, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  type BenchmarkUnavailableReason,
  getPortfolioPerformance,
  listBenchmarks,
} from "@/lib/api/client";
import { formatINR, formatPercent } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Sensitive } from "@/components/balance-visibility";
import { PickerButton } from "@/components/form/fields";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { Eyebrow } from "@/components/ui/eyebrow";
import { MONO } from "@/components/ui/table";
import { STALENESS_WARN_DAYS } from "@/lib/investments";

const FIGURE = "text-[20px] font-semibold leading-none tracking-[-0.02em]";
const TABULAR: CSSProperties = { fontVariantNumeric: "tabular-nums lining-nums" };
const DASH = <span className="text-muted-foreground">—</span>;

// A signed ratio (XIRR / alpha): green when positive (good), red when negative.
function signedRatio(value: number | null): { sign: string; color: string } {
  if (value == null) return { sign: "", color: "text-muted-foreground" };
  if (value > 0) return { sign: "+", color: "text-pos" };
  if (value < 0) return { sign: "−", color: "text-neg" };
  return { sign: "", color: "text-foreground" };
}

const REASON_TEXT: Record<BenchmarkUnavailableReason, string> = {
  no_portfolio_cashflows: "Record some investments to compare against the market.",
  no_benchmark_data:
    "This benchmark's NAV history hasn't been loaded yet — run the benchmark refresh.",
  as_of_before_inception:
    "This fund's history doesn't reach back far enough for the comparison.",
  negative_units:
    "Your withdrawals outweigh contributions in this counterfactual — alpha isn't meaningful here.",
  zero_terminal: "Not enough data to value the benchmark counterfactual.",
  unsolved: "The benchmark return couldn't be solved for these cashflows.",
};

export function PortfolioPerformance() {
  const [selectedId, setSelectedId] = useState<number | null>(null);

  const benchmarksQuery = useQuery({
    queryKey: ["benchmarks"],
    queryFn: listBenchmarks,
  });
  const perfQuery = useQuery({
    queryKey: ["portfolio", "performance", selectedId],
    queryFn: () => getPortfolioPerformance(selectedId ?? undefined),
  });

  if (perfQuery.status === "error") {
    return (
      <section className="rounded-lg border border-border bg-card">
        <div className="flex h-10 items-center px-4">
          <Eyebrow as="h2">Performance vs benchmark</Eyebrow>
        </div>
        <p className="px-4 pb-4 text-[13px] text-neg">
          Couldn’t load the benchmark comparison — is the API running?
        </p>
      </section>
    );
  }

  const perf = perfQuery.data;
  const ready = perf !== undefined;
  const benchmarks = benchmarksQuery.data ?? [];

  // The effective benchmark = the user's pick, else whatever the backend defaulted to.
  const effectiveId = selectedId ?? perf?.benchmark_id ?? null;
  const selectedName =
    benchmarks.find((b) => b.id === effectiveId)?.name ?? perf?.benchmark_name ?? "…";

  const reason = perf?.benchmark_unavailable_reason ?? null;
  const benchmarkSolved = ready && perf.benchmark_xirr != null;

  const portfolioXirr = signedRatio(perf?.portfolio_xirr ?? null);
  const benchmarkXirr = signedRatio(perf?.benchmark_xirr ?? null);
  const alpha = signedRatio(perf?.alpha ?? null);

  const gap = perf?.value_gap_paise ?? 0;
  const gapColor = gap > 0 ? "text-pos" : gap < 0 ? "text-neg" : "text-foreground";

  const notes = ready ? honestyNotes(perf) : [];

  return (
    <section className="rounded-lg border border-border bg-card">
      <div className="flex flex-col gap-3 px-4 pt-4 sm:flex-row sm:items-center sm:justify-between">
        <Eyebrow as="h2">Performance vs benchmark</Eyebrow>
        <div className="w-full sm:w-56">
          <PickerButton label={selectedName} muted={!ready}>
            {benchmarks.map((b) => (
              <DropdownMenuItem key={b.id} onSelect={() => setSelectedId(b.id)}>
                {b.name}
              </DropdownMenuItem>
            ))}
          </PickerButton>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4 px-4 pt-4 lg:grid-cols-4">
        <Tile label="Your XIRR">
          <Ratio ready={ready} value={perf?.portfolio_xirr ?? null} parts={portfolioXirr} />
        </Tile>

        <Tile label="Benchmark XIRR">
          <Ratio
            ready={ready && benchmarkSolved}
            value={perf?.benchmark_xirr ?? null}
            parts={benchmarkXirr}
          />
        </Tile>

        <Tile label="Alpha" sub={<span className="text-muted-foreground">vs the index fund</span>}>
          <Ratio ready={ready && perf.alpha != null} value={perf?.alpha ?? null} parts={alpha} />
        </Tile>

        <Tile
          label="The index would be"
          sub={
            ready && benchmarkSolved ? (
              <span className={gapColor}>
                {gap === 0
                  ? "level with you"
                  : gap > 0
                    ? "you’re ahead by "
                    : "you’re behind by "}
                {gap !== 0 ? <Sensitive>{formatINR(Math.abs(gap))}</Sensitive> : null}
              </span>
            ) : null
          }
        >
          <span className={cn(FIGURE, "text-foreground")} style={MONO}>
            {ready && benchmarkSolved ? (
              <Sensitive>{formatINR(perf.benchmark_value_paise)}</Sensitive>
            ) : (
              DASH
            )}
          </span>
        </Tile>
      </div>

      <div className="space-y-1 px-4 pb-4 pt-3 text-[11.5px] leading-relaxed">
        {ready ? (
          <p className="text-muted-foreground">
            vs {perf.benchmark_name} index fund — post-expense TRI NAV, not the raw index.
          </p>
        ) : null}
        {reason != null ? (
          <p className="text-muted-foreground">{REASON_TEXT[reason]}</p>
        ) : null}
        {notes.map((n) => (
          <p key={n} className="text-amber-600 dark:text-amber-500">
            {n}
          </p>
        ))}
      </div>
    </section>
  );
}

function Ratio({
  ready,
  value,
  parts,
}: {
  ready: boolean;
  value: number | null;
  parts: { sign: string; color: string };
}) {
  if (!ready || value == null) {
    return (
      <span className={cn(FIGURE, "tabular-nums text-muted-foreground")} style={TABULAR}>
        {DASH}
      </span>
    );
  }
  return (
    <span className={cn(FIGURE, "tabular-nums", parts.color)} style={TABULAR}>
      {parts.sign}
      {formatPercent(Math.abs(value))}
    </span>
  );
}

function Tile({
  label,
  children,
  sub,
}: {
  label: ReactNode;
  children: ReactNode;
  sub?: ReactNode;
}) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <Eyebrow>{label}</Eyebrow>
      <div className="mt-2">{children}</div>
      {sub != null ? <div className="mt-1.5 text-[11.5px] leading-none">{sub}</div> : null}
    </div>
  );
}

// PRD-required caveats (⚠), shown when they apply. Both staleness gates use
// STALENESS_WARN_DAYS — the calendar is defined once in
// backend/app/schemas/performance.py and mirrored in lib/investments.ts. Do NOT restate
// the reasoning here: the previous version of this comment re-derived "beyond a
// weekend's lag" as ≥ 3, which is the Friday→Monday distance, so the gate fired on
// exactly the case the comment said it excluded.
function honestyNotes(perf: {
  partial: boolean;
  benchmark_cache_stale: boolean;
  is_multi_asset: boolean;
  nav_staleness_days: number | null;
  fx_staleness_days: number | null;
  fx_unavailable_count: number;
}): string[] {
  const notes: string[] = [];
  if (perf.partial)
    notes.push(
      "⚠ The fund’s history starts after some of your investments — earlier buys are priced at its inception NAV.",
    );
  if (perf.benchmark_cache_stale)
    notes.push(
      "⚠ Some cashflows fall outside the cached NAV range — refresh benchmark NAVs for an exact comparison.",
    );
  if (perf.is_multi_asset)
    notes.push(
      "Your portfolio spans multiple asset classes; this single-asset index is a rough yardstick.",
    );
  // "Refresh, or update by hand" rather than "refresh them": for fd / bond / nps / gold /
  // other there is no price source, so refresh-navs returns them as `skipped` and the
  // advice to press sync is unactionable — and after step 20 those are exactly the
  // holdings whose real age this number finally reflects.
  if (
    perf.nav_staleness_days != null &&
    perf.nav_staleness_days >= STALENESS_WARN_DAYS
  )
    notes.push(
      `⚠ Your holdings’ NAVs are ${perf.nav_staleness_days} days behind — refresh, or update the hand-priced ones yourself, for an accurate alpha.`,
    );
  if (
    perf.fx_staleness_days != null &&
    perf.fx_staleness_days >= STALENESS_WARN_DAYS
  )
    notes.push(
      `⚠ The USD→INR rate used is ${perf.fx_staleness_days} days old — your USD holdings’ INR value has drifted since.`,
    );
  // Not "unpriced": these holdings have a NAV, they just can't be converted.
  if (perf.fx_unavailable_count > 0)
    notes.push(
      `⚠ ${perf.fx_unavailable_count} priced USD holding${perf.fx_unavailable_count === 1 ? "" : "s"} left out of this comparison — no USD→INR rate is cached.`,
    );
  return notes;
}
