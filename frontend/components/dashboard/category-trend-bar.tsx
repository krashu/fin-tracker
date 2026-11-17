"use client";

/**
 * Category trend (PRD §F8 view 6 — "how is my category mix shifting?"). A
 * monthly sign-stacked bar over the trailing 12 calendar months of
 * `GET /dashboards/category-trend`: the six largest parents, the rest folded
 * into one hatched "N more categories" series, Uncategorized its own last
 * series. Click a parent (legend, table row or bar segment) to drill into its
 * subcategories; "← All categories" comes back. Dashed lines mark each 1 January
 * (year start).
 *
 * The window is server-anchored on the IST business month, so the card ignores
 * the /spending period picker; only the tag filter (`labelId`) scopes it.
 * Archived subcategories roll into their parent server-side (ADR-0012 rule 6).
 *
 * Values stay signed: spend plots positive, a net-credit month dips below y=0
 * (`stackOffset="sign"`), nothing is clamped. The legend and the table twin are
 * always available — identity is never colour-alone, and the table is the
 * keyboard path to folded parents.
 */
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ReferenceLine,
  XAxis,
  YAxis,
} from "recharts";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { Pill } from "@/components/ui/pill";
import { Sensitive, useBalanceHidden } from "@/components/balance-visibility";
import { getCategoryTrend } from "@/lib/api/client";
import { periodLabel, yearBoundaries } from "@/lib/charts";
import {
  chartRows,
  drillSeries,
  overviewSeries,
  type TrendSeries,
} from "@/lib/category-trend";
import { compactINR, formatINR } from "@/lib/format";
import { periodLabel as longMonth } from "@/lib/period";
import { cn } from "@/lib/utils";

const MONTHS = 12;

type DrillSource = "legend" | "table" | "bar";

const HATCH_SWATCH =
  "repeating-linear-gradient(45deg, var(--muted-foreground) 0 1.5px, transparent 1.5px 4px)";

function Swatch({ series }: { series: TrendSeries }) {
  return (
    <span
      aria-hidden
      className="h-2.5 w-2.5 shrink-0 rounded-[2px] border border-border/60"
      style={
        series.pattern === "hatch"
          ? { background: HATCH_SWATCH }
          : { backgroundColor: series.color }
      }
    />
  );
}

/** One tooltip row: swatch, series name, amount. `ChartTooltipContent` hands
 * the whole row to `formatter`, dropping its default swatch + label, so the
 * row is rebuilt here exactly as the cashflow card does. */
export function TrendTooltipRow({
  series,
  value,
}: {
  series: TrendSeries | undefined;
  value: number;
}) {
  return (
    <div className="flex w-full items-center justify-between gap-3">
      <span className="flex items-center gap-1.5 text-muted-foreground">
        {series ? <Swatch series={series} /> : null}
        {series?.label ?? ""}
      </span>
      <span className="font-mono font-medium tabular-nums text-foreground">
        {formatINR(value)}
      </span>
    </div>
  );
}

export function CategoryTrendBar({ labelId }: { labelId?: number }) {
  const { hidden } = useBalanceHidden();
  const [drilledId, setDrilledId] = useState<number | null>(null);
  const [showTable, setShowTable] = useState(false);
  const opener = useRef<{ source: DrillSource; parentId: number } | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const backRef = useRef<HTMLButtonElement>(null);
  const foldHatchId = `fold-hatch-${useId().replace(/:/g, "")}`;

  const query = useQuery({
    queryKey: ["dashboards", "category-trend", { months: MONTHS, label_id: labelId }],
    queryFn: () => getCategoryTrend({ months: MONTHS, label_id: labelId }),
    // Refetch keeps the frame: switching tag holds the previous render
    // (dimmed) rather than flashing a skeleton.
    placeholderData: keepPreviousData,
  });
  const resp = query.data;
  // Derived, so a refetch that drops the drilled parent falls back to overview.
  const drilled =
    drilledId == null
      ? null
      : (resp?.parents.find((p) => p.parent_id === drilledId) ?? null);

  const series = useMemo(() => {
    if (!resp) return [];
    return drilled ? drillSeries(drilled) : overviewSeries(resp);
  }, [resp, drilled]);
  const tableSeries = useMemo(() => {
    if (!resp) return [];
    return drilled
      ? drillSeries(drilled, { fold: false })
      : overviewSeries(resp, { fold: false });
  }, [resp, drilled]);
  const rows = useMemo(
    () => (resp ? chartRows(resp.periods, series) : []),
    [resp, series],
  );
  const byKey = useMemo(() => new Map(series.map((s) => [s.key, s])), [series]);
  const chartConfig = useMemo(
    () =>
      Object.fromEntries(
        series.map((s) => [s.key, { label: s.label, color: s.color }]),
      ) satisfies ChartConfig,
    [series],
  );

  const drill = (parentId: number | null, source: DrillSource) => {
    if (parentId == null) return;
    opener.current = { source, parentId };
    setDrilledId(parentId);
  };

  // Focus: into the back button on drill; back to the opener (or the heading)
  // on return. The opener unmounts while drilled, so look it up by data
  // attribute rather than holding the element.
  const isDrilled = drilled != null;
  useEffect(() => {
    if (isDrilled) {
      backRef.current?.focus();
      return;
    }
    const o = opener.current;
    if (!o) return;
    opener.current = null;
    const source = o.source === "bar" ? "legend" : o.source;
    const target = rootRef.current?.querySelector<HTMLElement>(
      `[data-drill-source="${source}"][data-parent-id="${o.parentId}"]`,
    );
    (target ?? headingRef.current)?.focus();
  }, [isDrilled]);

  const empty = query.isSuccess && (resp?.parents.length ?? 0) === 0;
  const yearLines = resp ? yearBoundaries(resp.periods) : [];

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between border-b">
        <div className="flex items-center gap-2">
          <CardTitle as="h2" className="text-[14px]" tabIndex={-1} ref={headingRef}>
            Category trend
          </CardTitle>
          <span className="text-[12.5px] text-muted-foreground">
            · last {MONTHS} months
          </span>
        </div>
        {resp && !empty ? (
          <Pill
            active={showTable}
            aria-pressed={showTable}
            onClick={() => setShowTable((v) => !v)}
          >
            Table
          </Pill>
        ) : null}
      </CardHeader>

      <CardContent className="pt-4">
        <div ref={rootRef}>
          {query.isError ? (
            <p className="py-8 text-center text-[13px] text-neg">
              Couldn’t load — is the API running?
            </p>
          ) : empty ? (
            <p className="py-8 text-center text-[13px] text-muted-foreground">
              {labelId != null
                ? "No tagged spending in the last 12 months."
                : "No spending in the last 12 months."}
            </p>
          ) : resp ? (
            <div
              className={cn(
                "flex flex-col gap-3",
                query.isPlaceholderData && "opacity-60",
              )}
            >
              {drilled ? (
                <div className="flex items-center gap-3 text-[13px]">
                  <button
                    type="button"
                    ref={backRef}
                    onClick={() => setDrilledId(null)}
                    className="rounded-md border border-border px-2 py-1 text-[12px] text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    ← All categories
                  </button>
                  <span className="font-medium">
                    {drilled.parent_name ?? "Category"}
                    {drilled.archived ? " (archived)" : ""}
                  </span>
                </div>
              ) : null}

              {showTable ? (
                <TrendTable
                  periods={resp.periods}
                  series={tableSeries}
                  onDrill={drilled ? undefined : (id) => drill(id, "table")}
                />
              ) : (
                <ChartContainer
                  config={chartConfig}
                  role="img"
                  aria-label="Monthly spend by category, last 12 months. Use the legend or the table to drill into a category."
                  data-testid="trend-chart"
                  className={cn(
                    "aspect-auto h-[300px] w-full",
                    // Blur the bars + Y-axis amounts and disable the tooltip so
                    // no magnitude leaks while balances are hidden.
                    hidden && "pointer-events-none select-none blur-sm",
                  )}
                >
                  <BarChart
                    data={rows}
                    stackOffset="sign"
                    margin={{ left: 4, right: 4, top: 4 }}
                  >
                    <defs>
                      <pattern
                        id={foldHatchId}
                        patternUnits="userSpaceOnUse"
                        width="6"
                        height="6"
                        patternTransform="rotate(45)"
                      >
                        <rect width="6" height="6" fill="var(--card)" />
                        <line
                          x1="0"
                          y1="0"
                          x2="0"
                          y2="6"
                          stroke="var(--muted-foreground)"
                          strokeWidth="2"
                        />
                      </pattern>
                    </defs>
                    <CartesianGrid vertical={false} stroke="var(--border)" />
                    <XAxis
                      dataKey="period"
                      tickLine={false}
                      axisLine={false}
                      tickMargin={8}
                      minTickGap={8}
                      className="text-[10px]"
                      tickFormatter={(p: string) => periodLabel(p, "month")}
                    />
                    <YAxis
                      tickLine={false}
                      axisLine={false}
                      width={48}
                      tickFormatter={(v: number) => compactINR(v)}
                      className="text-[10px]"
                    />
                    <ReferenceLine y={0} stroke="var(--border)" strokeWidth={1} />
                    {yearLines.map((b) => (
                      <ReferenceLine
                        key={b.period}
                        x={b.period}
                        position="start"
                        strokeDasharray="4 4"
                        stroke="var(--muted-foreground)"
                        label={{
                          value: b.label,
                          position: "insideTopLeft",
                          fontSize: 10,
                          fill: "var(--muted-foreground)",
                        }}
                      />
                    ))}
                    <ChartTooltip
                      cursor={{ fill: "var(--muted)" }}
                      content={
                        <ChartTooltipContent
                          labelFormatter={(_, payload) => {
                            const p = payload?.[0]?.payload?.period;
                            if (typeof p !== "string") return null;
                            const [y, m] = p.split("-").map(Number);
                            return longMonth({ year: y, mon: m });
                          }}
                          formatter={(value, name) => (
                            <TrendTooltipRow
                              series={byKey.get(String(name))}
                              value={Number(value)}
                            />
                          )}
                        />
                      }
                    />
                    {series.map((s) => (
                      <Bar
                        key={s.key}
                        dataKey={s.key}
                        name={s.key}
                        stackId="trend"
                        fill={
                          s.pattern === "hatch" ? `url(#${foldHatchId})` : s.color
                        }
                        // The surface-coloured stroke is the 2px gap between
                        // stacked segments. Segments stay square: a rounded
                        // data-end doesn't map onto a sign-offset stack.
                        stroke="var(--card)"
                        strokeWidth={2}
                        maxBarSize={24}
                        cursor={s.drillable ? "pointer" : undefined}
                        onClick={
                          s.drillable
                            ? () => drill(s.categoryId, "bar")
                            : undefined
                        }
                      />
                    ))}
                  </BarChart>
                </ChartContainer>
              )}

              <ul className="flex flex-wrap gap-x-4 gap-y-1.5 text-[12px] text-muted-foreground">
                {series.map((s) => (
                  <li key={s.key} className="flex items-center gap-1.5">
                    <Swatch series={s} />
                    {s.drillable && !drilled ? (
                      <button
                        type="button"
                        data-drill-source="legend"
                        data-parent-id={s.categoryId ?? undefined}
                        aria-label={`Show ${s.label} subcategories`}
                        onClick={() => drill(s.categoryId, "legend")}
                        className="rounded-sm outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        {s.label}
                      </button>
                    ) : (
                      <span>{s.label}</span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <div className="h-[300px]" aria-busy />
          )}
        </div>
      </CardContent>
    </Card>
  );
}

/** The table twin of the chart: every series unfolded, months across, with a
 * Total. In the overview each parent's name is a button — the keyboard path to
 * a parent the chart folded away. */
function TrendTable({
  periods,
  series,
  onDrill,
}: {
  periods: string[];
  series: TrendSeries[];
  onDrill?: (parentId: number) => void;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-[12px]">
        <caption className="sr-only">
          Monthly spend by category over the last 12 months
        </caption>
        <thead>
          <tr className="text-left text-muted-foreground">
            <th scope="col" className="py-1 pr-3 font-medium">
              Category
            </th>
            {periods.map((p) => {
              const [y, m] = p.split("-").map(Number);
              return (
                <th
                  key={p}
                  scope="col"
                  title={longMonth({ year: y, mon: m })}
                  className="px-2 py-1 text-right font-medium"
                >
                  {periodLabel(p, "month")}
                </th>
              );
            })}
            <th scope="col" className="pl-3 py-1 text-right font-medium">
              Total
            </th>
          </tr>
        </thead>
        <tbody>
          {series.map((s) => (
            <tr key={s.key} className="border-t border-border">
              <th scope="row" className="py-1 pr-3 text-left font-normal">
                <span className="flex items-center gap-1.5">
                  <Swatch series={s} />
                  {onDrill && s.drillable && s.categoryId != null ? (
                    <button
                      type="button"
                      data-drill-source="table"
                      data-parent-id={s.categoryId}
                      onClick={() => onDrill(s.categoryId as number)}
                      className="rounded-sm text-left outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      {s.label}
                    </button>
                  ) : (
                    s.label
                  )}
                </span>
              </th>
              {s.totalsPaise.map((v, i) => (
                <td
                  key={periods[i]}
                  className="px-2 py-1 text-right font-mono tabular-nums"
                >
                  <Sensitive>{formatINR(0 - v)}</Sensitive>
                </td>
              ))}
              <td className="pl-3 py-1 text-right font-mono font-medium tabular-nums">
                <Sensitive>
                  {formatINR(0 - s.totalsPaise.reduce((a, b) => a + b, 0))}
                </Sensitive>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
