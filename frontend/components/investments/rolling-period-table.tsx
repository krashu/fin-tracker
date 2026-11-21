"use client";

/**
 * Rolling Period Performance Table (Milestone 2 Phase 5).
 *
 * Detailed breakdown across 1M, 3M, 6M, 1Y, 3Y, 5Y, and All horizons:
 * - Time-Weighted Return (TWR) and Annualized TWR (CAGR)
 * - Money-Weighted Return (Portfolio XIRR) vs. Benchmark XIRR
 * - Excess Return / Alpha (α)
 * - Risk Metrics: Maximum Drawdown (%) and Annualized Volatility (%)
 * - Active trading days
 *
 * Allows one-click switching of the active period for the dashboard charts.
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { MONO, Td, Th } from "@/components/ui/table";
import { formatPercent } from "@/lib/format";
import type { PeriodPerformance } from "@/lib/api/portfolio-timeseries";
import { ROLLING_PERIODS, type RollingPeriod } from "@/lib/investments";
import { cn } from "@/lib/utils";

const DASH = <span className="text-muted-foreground">—</span>;

function signedColor(value: number | null): string {
  if (value == null) return "text-muted-foreground";
  if (value > 0) return "text-pos";
  if (value < 0) return "text-neg";
  return "text-foreground";
}

export type RollingPeriodTableProps = {
  periods: PeriodPerformance[];
  selectedPeriod: RollingPeriod;
  onSelectPeriod: (period: RollingPeriod) => void;
  benchmarkName?: string | null;
  className?: string;
};

export function RollingPeriodTable({
  periods,
  selectedPeriod,
  onSelectPeriod,
  benchmarkName = "Benchmark",
  className,
}: RollingPeriodTableProps) {
  // Map periods by key ("1M", "3M", etc.)
  const periodMap = new Map<string, PeriodPerformance>();
  periods.forEach((p) => periodMap.set(p.period, p));

  return (
    <Card className={cn("overflow-hidden", className)}>
      <CardHeader className="flex flex-col gap-1 pb-3">
        <CardTitle className="text-base font-semibold">
          Rolling Period Breakdown
        </CardTitle>
        <p className="text-[12px] text-muted-foreground">
          Standardized performance and risk parameters across fixed rolling horizons
          vs. {benchmarkName}
        </p>
      </CardHeader>

      <CardContent className="p-0">
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-[12.5px]">
            <thead>
              <tr className="border-b border-border">
                <Th first>Period</Th>
                <Th align="right">TWR</Th>
                <Th align="right">Annualized TWR</Th>
                <Th align="right">XIRR</Th>
                <Th align="right">Benchmark XIRR</Th>
                <Th align="right">Alpha (α)</Th>
                <Th align="right">Max Drawdown</Th>
                <Th align="right">Ann. Volatility</Th>
                <Th align="right" last>
                  Active Days
                </Th>
              </tr>
            </thead>
            <tbody>
              {ROLLING_PERIODS.map((periodKey, idx) => {
                const row = periodMap.get(periodKey);
                const isSelected = selectedPeriod === periodKey;
                const isLast = idx === ROLLING_PERIODS.length - 1;
                const borderClass = isLast ? "" : "border-b border-border/50";

                const twrPct = row?.twr_pct ?? null;
                const twrAnn = row?.twr_annualized ?? null;
                const pXirr = row?.xirr ?? null;
                const bXirr = row?.benchmark_xirr ?? null;
                const alpha = row?.alpha ?? null;
                const maxDd = row?.max_drawdown ?? 0;
                const vol = row?.volatility_annualized ?? null;
                const activeDays = row?.active_days ?? 0;

                return (
                  <tr
                    key={periodKey}
                    onClick={() => onSelectPeriod(periodKey)}
                    className={cn(
                      "cursor-pointer transition-colors hover:bg-muted/60",
                      isSelected && "bg-accent/40 font-medium",
                    )}
                  >
                    {/* Period label */}
                    <Td first borderClass={borderClass}>
                      <div className="flex items-center gap-2">
                        <span
                          className={cn(
                            "inline-flex h-5 items-center justify-center rounded px-1.5 font-mono text-[11px] font-semibold",
                            isSelected
                              ? "bg-primary text-primary-foreground"
                              : "bg-muted text-foreground",
                          )}
                        >
                          {periodKey === "all" ? "All" : periodKey}
                        </span>
                        {isSelected ? (
                          <span className="text-[10.5px] text-muted-foreground font-normal">
                            (active)
                          </span>
                        ) : null}
                      </div>
                    </Td>

                    {/* TWR */}
                    <Td align="right" borderClass={borderClass}>
                      <span
                        className={cn("tabular-nums", signedColor(twrPct))}
                        style={MONO}
                      >
                        {twrPct != null ? (
                          `${twrPct >= 0 ? "+" : ""}${formatPercent(twrPct)}`
                        ) : (
                          DASH
                        )}
                      </span>
                    </Td>

                    {/* Annualized TWR */}
                    <Td align="right" borderClass={borderClass}>
                      <span
                        className={cn("tabular-nums", signedColor(twrAnn))}
                        style={MONO}
                      >
                        {twrAnn != null ? (
                          `${twrAnn >= 0 ? "+" : ""}${formatPercent(twrAnn)} p.a.`
                        ) : (
                          DASH
                        )}
                      </span>
                    </Td>

                    {/* Portfolio XIRR */}
                    <Td align="right" borderClass={borderClass}>
                      <span
                        className={cn("tabular-nums", signedColor(pXirr))}
                        style={MONO}
                      >
                        {pXirr != null ? (
                          `${pXirr >= 0 ? "+" : ""}${formatPercent(pXirr)}`
                        ) : (
                          DASH
                        )}
                      </span>
                    </Td>

                    {/* Benchmark XIRR */}
                    <Td align="right" borderClass={borderClass}>
                      <span
                        className={cn("tabular-nums", signedColor(bXirr))}
                        style={MONO}
                      >
                        {bXirr != null ? (
                          `${bXirr >= 0 ? "+" : ""}${formatPercent(bXirr)}`
                        ) : (
                          DASH
                        )}
                      </span>
                    </Td>

                    {/* Alpha */}
                    <Td align="right" borderClass={borderClass}>
                      <span
                        className={cn("tabular-nums font-semibold", signedColor(alpha))}
                        style={MONO}
                      >
                        {alpha != null ? (
                          `${alpha >= 0 ? "+" : ""}${(alpha * 100).toFixed(2)}%`
                        ) : (
                          DASH
                        )}
                      </span>
                    </Td>

                    {/* Max Drawdown */}
                    <Td align="right" borderClass={borderClass}>
                      <span
                        className={cn(
                          "tabular-nums",
                          maxDd < 0 ? "text-neg" : "text-foreground",
                        )}
                        style={MONO}
                      >
                        {maxDd !== 0 ? (
                          `${(maxDd * 100).toFixed(2)}%`
                        ) : (
                          "0.00%"
                        )}
                      </span>
                    </Td>

                    {/* Ann. Volatility */}
                    <Td align="right" borderClass={borderClass}>
                      <span className="tabular-nums text-foreground" style={MONO}>
                        {vol != null ? formatPercent(vol) : DASH}
                      </span>
                    </Td>

                    {/* Active Days */}
                    <Td align="right" last borderClass={borderClass}>
                      <span className="tabular-nums text-muted-foreground" style={MONO}>
                        {activeDays}
                      </span>
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}
