"use client";

/**
 * Spend by tag over time, as a HEATMAP (PRD §F3a labels; tag-analysis arc Phase C
 * — "when / how concentrated is spend under a tag?"). Replaces the earlier
 * multi-line trend chart, which capped at the top 8 tags by 12-month total: a tag
 * with a large but *brief* one-off spend lost the top-8-by-total race and was
 * dropped entirely — hiding exactly the concentrated spend you'd most want to
 * notice. A heatmap gives every tag its own row (no truncation) and renders a
 * big-but-brief spend as one dark cell in an otherwise faint row, so it pops
 * instead of being averaged into a total.
 *
 * Rows = tags (server-sorted biggest overall spender first), columns = the
 * trailing 12 calendar months of `GET /dashboards/spend-by-tag-by-period` (the
 * SAME payload the old line chart fetched — a dense, zero-filled tag×period grid
 * with NO server cap). This is intentionally a plain `<table>`, not Recharts:
 * Recharts has no heatmap primitive, and a table gives screen readers the
 * row/column association for free.
 *
 * Design decisions that follow from tags being **many:many** (a txn can carry
 * `#travel` + `#work`) where category is 1:1:
 *
 *  1. **Never a stack / total.** Each cell is an independent signed magnitude; the
 *     per-tag cells double-count multi-tagged txns, so they don't sum to a month
 *     total (same reason the line chart plotted independent lines, arc decision #3).
 *  2. **GLOBAL normalization, qualitative scale.** Intensity is the cell's
 *     magnitude relative to the biggest cell anywhere in the grid — so a dark cell
 *     means "big spend *anywhere*", honoring the no-false-total constraint. The
 *     tradeoff: one outlier sets the max and pushes the rest toward the faint bins.
 *     So this is a *concentration* view; precise "how much" lives on the adjacent
 *     `SpendByTag` ranked bar. With sqrt + global norm the bins map to no fixed
 *     rupee range — color reads relative concentration, never an absolute amount.
 *  3. **Signed net, never clamped.** A net-credit cell (refunds > spend, rare)
 *     flips hue to `--pos` green; a cell that nets to ~0 (spend fully refunded)
 *     renders blank — parity with `SpendByTag` and the old line chart's y=0.
 *
 * Colour derives intensity from the LOCKED `--primary` (indigo) via `color-mix`
 * toward transparent — it adds no new palette token (globals.css "PALETTE LOCKED"
 * rule). `--primary` is per-theme, so the ramp reads in light and dark unchanged.
 *
 * Owns its own window, independent of the other /spending cards, and is
 * intentionally UNSCOPED by the /spending tag cross-filter (it IS the tag view).
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { listSpendByTagByPeriod } from "@/lib/api/client";
import { trailingMonths } from "@/lib/dates";
import { formatINR } from "@/lib/format";
import { MONTH_ABBR } from "@/lib/charts";
import { labelDisplay } from "@/lib/labels";
import { cn } from "@/lib/utils";
import { useBalanceHidden } from "@/components/balance-visibility";

// color-mix percentage of --primary (or --pos) per intensity bin. Index = bin
// (0 = empty, no fill). Bins 1..4 climb; bin 4 is the full token.
const BIN_MIX_PCT = [0, 18, 42, 68, 100] as const;

// 5 bins: 0 = no activity, 1..4 = increasing intensity. sqrt lifts small values
// so they still climb the bins and one outlier doesn't crush the grid into bin 1.
function binOf(mag: number, maxMag: number): number {
  if (mag === 0 || maxMag === 0) return 0;
  const s = Math.sqrt(mag / maxMag);
  if (s <= 0.25) return 1;
  if (s <= 0.5) return 2;
  if (s <= 0.75) return 3;
  return 4;
}

// Cell fill for a signed total. Spend (negative) → indigo ramp; net-credit
// (positive, rare) → green ramp; zero → undefined (no fill, faint border shows).
function cellFill(totalPaise: number, maxMag: number): string | undefined {
  const bin = binOf(Math.abs(totalPaise), maxMag);
  if (bin === 0) return undefined;
  const base = totalPaise > 0 ? "var(--pos)" : "var(--primary)";
  return `color-mix(in oklab, ${base} ${BIN_MIX_PCT[bin]}%, transparent)`;
}

// "2026-03" → "Mar 2026" (accessible name / hover caption — always unambiguous).
function monthYear(period: string): string {
  const [y, mm] = period.split("-");
  return `${MONTH_ABBR[Number(mm)] ?? mm} ${y}`;
}

// Compact column header — the year is shown only where the calendar year flips
// (January) or on the first column, so a window crossing a year boundary isn't
// ambiguous in the sighted header (the accessible name always carries the year).
function monthHeader(period: string, isFirst: boolean): string {
  const [y, mm] = period.split("-");
  const m = Number(mm);
  const base = MONTH_ABBR[m] ?? period;
  return m === 1 || isFirst ? `${base} '${y.slice(2)}` : base;
}

// Accessible name for a cell. HIDDEN-AWARE: drops the amount when balances are
// hidden, so the exact ₹ never leaks to assistive tech / on focus despite the
// visual blur (blur-sm / pointer-events-none are visual + mouse only).
function cellName(
  tagName: string,
  period: string,
  totalPaise: number,
  hidden: boolean,
): string {
  const base = `${labelDisplay(tagName)} · ${monthYear(period)}`;
  if (hidden) return base;
  if (totalPaise === 0) return `${base} — no spend`;
  if (totalPaise > 0) return `${base} — net credit ${formatINR(totalPaise)}`;
  return `${base} — ${formatINR(-totalPaise)}`;
}

type ActiveCell = { tagName: string; period: string; totalPaise: number };

export function SpendByTagHeatmap({ year }: { year: number }) {
  const { hidden } = useBalanceHidden();
  const [active, setActive] = useState<ActiveCell | null>(null);

  const start = `${year}-01-01`;
  const end = `${year}-12-31`;
  const query = useQuery({
    queryKey: [
      "dashboards",
      "spend-by-tag-by-period",
      { bucket: "month", start, end },
    ],
    queryFn: () => listSpendByTagByPeriod({ bucket: "month", start, end }),
  });

  const tags = query.data?.tags ?? [];
  const buckets = query.data?.buckets ?? [];
  const periods = useMemo(() => buckets.map((b) => b.period), [buckets]);

  // Map<label_id, Map<period, total_paise>> for O(1) cell lookup, plus the global
  // max magnitude (normalization denominator) and whether any net-credit exists
  // (drives the legend's green swatch). One pass over the dense grid.
  const { cells, maxMag, hasCredit } = useMemo(() => {
    const cells = new Map<number, Map<string, number>>();
    let maxMag = 0;
    let hasCredit = false;
    for (const b of buckets) {
      for (const c of b.totals) {
        let inner = cells.get(c.label_id);
        if (!inner) {
          inner = new Map();
          cells.set(c.label_id, inner);
        }
        inner.set(b.period, c.total_paise);
        maxMag = Math.max(maxMag, Math.abs(c.total_paise));
        if (c.total_paise > 0) hasCredit = true;
      }
    }
    return { cells, maxMag, hasCredit };
  }, [buckets]);

  return (
    <Card>
      <CardHeader className="border-b">
        <div className="flex items-center gap-2">
          <CardTitle as="h2" className="text-[14px]">
            Tag spend over time
          </CardTitle>
          <span className="text-[12.5px] text-muted-foreground">· {year}</span>
        </div>
        <p className="text-[11.5px] text-muted-foreground">
          Darker = more spend under a tag that month. Tags overlap, so they don’t
          sum to a total.
        </p>
      </CardHeader>

      <CardContent className="pt-4">
        {query.isPending ? (
          <p className="py-8 text-center text-[13px] text-muted-foreground">
            Loading…
          </p>
        ) : query.isError ? (
          <p className="py-8 text-center text-[13px] text-neg">
            Couldn’t load — is the API running?
          </p>
        ) : tags.length === 0 ? (
          <p className="py-8 text-center text-[13px] text-muted-foreground">
            No tagged spending in the last 12 months.
          </p>
        ) : (
          <>
            <div
              className={cn(
                "max-h-[420px] overflow-auto",
                // Blur the grid + disable pointer interaction so no magnitude
                // leaks while balances are hidden (the cell names also drop the
                // amount, and the hover caption is suppressed, below).
                hidden && "pointer-events-none select-none blur-sm",
              )}
            >
              <table className="border-separate border-spacing-1 text-[11px]">
                <caption className="sr-only">
                  Spend per tag per month over the last 12 months; cell color
                  encodes spend magnitude and sign. Tags overlap and do not sum to
                  a total.
                </caption>
                <thead>
                  <tr>
                    <th
                      scope="col"
                      className="sticky left-0 top-0 z-20 bg-card"
                    />
                    {periods.map((p, i) => (
                      <th
                        key={p}
                        scope="col"
                        className="sticky top-0 z-10 w-9 bg-card pb-1 text-center font-normal text-muted-foreground tabular"
                      >
                        {monthHeader(p, i === 0)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {tags.map((t) => {
                    const row = cells.get(t.label_id);
                    return (
                      <tr key={t.label_id}>
                        <th
                          scope="row"
                          className="sticky left-0 z-10 bg-card pr-2 text-right font-normal text-foreground"
                        >
                          <div
                            className="max-w-[130px] truncate"
                            title={labelDisplay(t.label_name)}
                          >
                            {labelDisplay(t.label_name)}
                          </div>
                        </th>
                        {periods.map((p) => {
                          const total = row?.get(p) ?? 0;
                          const fill = cellFill(total, maxMag);
                          const name = cellName(t.label_name, p, total, hidden);
                          return (
                            <td key={p} className="p-0">
                              <div
                                role="img"
                                tabIndex={0}
                                aria-label={name}
                                title={name}
                                onMouseEnter={() =>
                                  !hidden &&
                                  setActive({
                                    tagName: t.label_name,
                                    period: p,
                                    totalPaise: total,
                                  })
                                }
                                onFocus={() =>
                                  !hidden &&
                                  setActive({
                                    tagName: t.label_name,
                                    period: p,
                                    totalPaise: total,
                                  })
                                }
                                onMouseLeave={() => setActive(null)}
                                onBlur={() => setActive(null)}
                                className={cn(
                                  "h-7 w-9 rounded-sm outline-offset-1",
                                  fill ? null : "border border-border/60",
                                )}
                                style={
                                  fill ? { backgroundColor: fill } : undefined
                                }
                              />
                            </td>
                          );
                        })}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {/* Legend + hover/focus detail. The scale is qualitative (sqrt +
                global norm → no fixed rupee tiers); the exact amount comes from
                hovering/focusing a cell. Detail is suppressed when hidden so it
                can't leak an amount on focus. */}
            <div className="mt-3 flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
              <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                <span>less</span>
                <span className="flex gap-1">
                  {[1, 2, 3, 4].map((bin) => (
                    <span
                      key={bin}
                      className="h-3 w-3 rounded-sm border border-border/60"
                      style={{
                        backgroundColor: `color-mix(in oklab, var(--primary) ${BIN_MIX_PCT[bin]}%, transparent)`,
                      }}
                    />
                  ))}
                </span>
                <span>more</span>
                {hasCredit ? (
                  <span className="ml-2 flex items-center gap-1">
                    <span
                      className="h-3 w-3 rounded-sm border border-border/60"
                      style={{ backgroundColor: "var(--pos)" }}
                    />
                    net credit
                  </span>
                ) : null}
              </div>

              <p className="text-[11.5px] text-muted-foreground tabular">
                {hidden ? (
                  "Amounts hidden."
                ) : active ? (
                  <>
                    <span className="text-foreground">
                      {labelDisplay(active.tagName)}
                    </span>{" "}
                    · {monthYear(active.period)} —{" "}
                    {active.totalPaise === 0
                      ? "no spend"
                      : active.totalPaise > 0
                        ? `net credit ${formatINR(active.totalPaise)}`
                        : formatINR(-active.totalPaise)}
                  </>
                ) : (
                  "Hover or focus a cell for the exact amount."
                )}
              </p>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
