"use client";

/**
 * Investment portfolio dashboard (PRD §F8 view 6) — four KPI tiles plus an
 * asset-class allocation donut, from GET /portfolio/summary.
 *
 * Money (value / invested / unrealized P&L) is over NAV-bearing holdings only
 * (null-NAV count ₹0, per the cross-part rule); the unpriced count is surfaced
 * as a footnote on the value tile. Returns (XIRR) and allocation % are ratios,
 * not magnitudes, so they are never masked by the hide-balance toggle — and the
 * donut is fed **percentages, not rupees**, so it needs no blur either (only the
 * ₹ tiles wrap <Sensitive>).
 */
import type { CSSProperties, ReactNode } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Pie, PieChart } from "recharts";

import { getPortfolioSummary } from "@/lib/api/client";
import { formatINR, formatPercent } from "@/lib/format";
import { ASSET_CLASS_LABELS } from "@/lib/investments";
import { cn } from "@/lib/utils";
import { Sensitive } from "@/components/balance-visibility";
import { Eyebrow } from "@/components/ui/eyebrow";
import { MONO } from "@/components/ui/table";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";

// 20px figure recipe shared by every tile (matches the overview PortfolioCard).
const FIGURE = "text-[20px] font-semibold leading-none tracking-[-0.02em]";
// Hanken tabular for ratio tiles (XIRR / %); money tiles use MONO instead.
const TABULAR: CSSProperties = {
  fontVariantNumeric: "tabular-nums lining-nums",
};
// Up to 6 distinct slice colors; classes past the 6th wrap (≤9 asset classes
// exist and a real portfolio rarely spans more than a handful).
const sliceColor = (i: number) => `var(--chart-${(i % 6) + 1})`;

const DASH = <span className="text-muted-foreground">—</span>;

export function PortfolioSummary() {
  const query = useQuery({
    queryKey: ["portfolio", "summary"],
    queryFn: getPortfolioSummary,
  });

  if (query.status === "error") {
    return (
      <p className="mt-6 py-16 text-center text-[13px] text-neg">
        Couldn’t load your portfolio — is the API running?
      </p>
    );
  }

  const data = query.data;
  const ready = data !== undefined;

  const pnl = data?.unrealized_pnl_paise ?? 0;
  const invested = data?.invested_paise ?? 0;
  const xirr = data?.xirr ?? null;

  // Every holding unpriced: value is ₹0 by the null-NAV rule, so pnl = −invested
  // and pnl/invested = −100% — a misleading "total loss" when we simply can't
  // value the positions yet (the "N unpriced (₹0)" footnote explains the ₹0). Show
  // "—" for both the figure and the % in that case. Empty portfolio (invested 0)
  // is unaffected (still ₹0); partial-unpriced is left to the backend contract.
  const allUnpriced = ready && data.current_value_paise === 0 && invested > 0;

  const pnlSign = pnl > 0 ? "+" : pnl < 0 ? "−" : "";
  const pnlColor =
    pnl > 0 ? "text-pos" : pnl < 0 ? "text-neg" : "text-foreground";
  // Unrealized % is meaningless without a cost basis (invested 0) or a current
  // value (all unpriced) — suppress in both cases.
  const pnlPct = ready && invested > 0 && !allUnpriced ? pnl / invested : null;

  const xirrSign = xirr == null ? "" : xirr > 0 ? "+" : xirr < 0 ? "−" : "";
  const xirrColor =
    xirr == null
      ? "text-muted-foreground"
      : xirr > 0
        ? "text-pos"
        : xirr < 0
          ? "text-neg"
          : "text-foreground";

  return (
    <div className="mt-6 flex flex-col gap-5">
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Tile
          label="Current value"
          sub={
            ready ? (
              <span className="text-muted-foreground">
                {data.holdings_count} holding
                {data.holdings_count === 1 ? "" : "s"}
                {data.null_nav_count > 0
                  ? ` · ${data.null_nav_count} unpriced (₹0)`
                  : ""}
                {/* Deliberately not "unpriced": these holdings DO have a NAV —
                    they're excluded because no USD→INR rate is cached. */}
                {data.fx_unavailable_count > 0
                  ? ` · ${data.fx_unavailable_count} excluded (no FX rate)`
                  : ""}
              </span>
            ) : null
          }
        >
          <span className={cn(FIGURE, "text-foreground")} style={MONO}>
            {ready ? (
              <Sensitive>{formatINR(data.current_value_paise)}</Sensitive>
            ) : (
              DASH
            )}
          </span>
        </Tile>

        <Tile label="Invested">
          <span className={cn(FIGURE, "text-foreground")} style={MONO}>
            {ready ? <Sensitive>{formatINR(invested)}</Sensitive> : DASH}
          </span>
        </Tile>

        <Tile
          label="Unrealized P&L"
          sub={
            pnlPct == null ? null : (
              <span className={pnlColor}>
                {pnlSign}
                {formatPercent(Math.abs(pnlPct))}
              </span>
            )
          }
        >
          <span className={cn(FIGURE, pnlColor)} style={MONO}>
            {ready && !allUnpriced ? (
              <Sensitive>
                {pnlSign}
                {formatINR(Math.abs(pnl))}
              </Sensitive>
            ) : (
              DASH
            )}
          </span>
        </Tile>

        <Tile
          label="XIRR"
          sub={
            <span className="text-muted-foreground">on current holdings</span>
          }
        >
          <span
            className={cn(FIGURE, "tabular-nums", xirrColor)}
            style={TABULAR}
          >
            {!ready || xirr == null ? (
              DASH
            ) : (
              <>
                {xirrSign}
                {formatPercent(Math.abs(xirr))}
              </>
            )}
          </span>
        </Tile>
      </div>

      <AllocationDonut
        allocations={data?.allocations ?? []}
        ready={ready}
        // All THREE buckets, because they are disjoint and exhaustive over the
        // holdings set (holdings_service._rollup): priced, unpriced, and
        // priced-but-no-FX-rate. Omitting fx_unavailable_count drew "No
        // investments yet — record a buy" directly under a tile reading
        // "0 holdings · 1 excluded (no FX rate)".
        hasHoldings={
          (data?.holdings_count ?? 0) +
            (data?.null_nav_count ?? 0) +
            (data?.fx_unavailable_count ?? 0) >
          0
        }
      />
    </div>
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
      {sub != null ? (
        <div className="mt-1.5 text-[11.5px] leading-none">{sub}</div>
      ) : null}
    </div>
  );
}

function AllocationDonut({
  allocations,
  ready,
  hasHoldings,
}: {
  allocations: {
    asset_class: keyof typeof ASSET_CLASS_LABELS;
    value_paise: number;
  }[];
  ready: boolean;
  hasHoldings: boolean;
}) {
  const total = allocations.reduce((s, a) => s + a.value_paise, 0);
  // Slice value is the FRACTION (not paise) so no rupee magnitude ever enters
  // the chart DOM / tooltip — nothing to mask under the hide-balance toggle.
  const slices =
    total > 0
      ? allocations.map((a, i) => ({
          key: a.asset_class,
          label: ASSET_CLASS_LABELS[a.asset_class],
          fraction: a.value_paise / total,
          fill: sliceColor(i),
        }))
      : [];

  return (
    <section className="rounded-lg border border-border bg-card">
      <div className="flex h-10 items-center px-4">
        <Eyebrow as="h2">Allocation by asset class</Eyebrow>
      </div>
      {!ready ? (
        <p className="px-4 pb-4 text-[13px] text-muted-foreground">Loading…</p>
      ) : slices.length === 0 ? (
        <p className="px-4 pb-4 text-[13px] text-muted-foreground">
          {hasHoldings ? (
            "No priced holdings yet — add a NAV on an instrument to see allocation."
          ) : (
            <>
              No investments yet — record a buy on{" "}
              <Link
                href="/investments"
                className="font-medium text-primary hover:underline"
              >
                Transactions
              </Link>
              .
            </>
          )}
        </p>
      ) : (
        <div className="flex flex-col items-center gap-5 px-4 pb-5 sm:flex-row sm:gap-7">
          <ChartContainer
            config={{} satisfies ChartConfig}
            className="aspect-auto h-[176px] w-[176px] shrink-0"
          >
            <PieChart>
              <ChartTooltip
                content={
                  <ChartTooltipContent
                    hideLabel
                    formatter={(value, name) =>
                      `${name} · ${formatPercent(Number(value))}`
                    }
                  />
                }
              />
              <Pie
                data={slices}
                dataKey="fraction"
                nameKey="label"
                innerRadius={46}
                outerRadius={78}
                paddingAngle={1.5}
                stroke="var(--card)"
                strokeWidth={2}
              />
            </PieChart>
          </ChartContainer>
          <ul className="w-full flex-1 space-y-1.5">
            {slices.map((s) => (
              <li key={s.key} className="flex items-center gap-2 text-[12.5px]">
                <span
                  className="size-2.5 shrink-0 rounded-[2px]"
                  style={{ backgroundColor: s.fill }}
                />
                <span className="flex-1 truncate text-foreground/80">
                  {s.label}
                </span>
                <span
                  className="tabular-nums text-muted-foreground"
                  style={TABULAR}
                >
                  {formatPercent(s.fraction)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
