"use client";

/**
 * Key Performance Indicators Stat Tiles (Milestone 2 Phase 5).
 *
 * Displays:
 * 1. Current Value & Total Unrealized Gain/Loss (INR & %)
 * 2. Period TWR (% and Annualized CAGR when >= 1Y)
 * 3. Money-weighted XIRR vs. Benchmark XIRR & Alpha (α)
 * 4. Risk Metrics: Max Drawdown (%) and Annualized Volatility (%)
 */

import type { CSSProperties, ReactNode } from "react";

import { Sensitive } from "@/components/balance-visibility";
import { Eyebrow } from "@/components/ui/eyebrow";
import { MONO } from "@/components/ui/table";
import { formatINR, formatPercent } from "@/lib/format";
import type {
  PeriodPerformance,
  PortfolioTimeseriesPoint,
} from "@/lib/api/portfolio-timeseries";
import type { RollingPeriod } from "@/lib/investments";
import { cn } from "@/lib/utils";

const FIGURE = "text-[20px] font-semibold leading-none tracking-[-0.02em]";
const TABULAR: CSSProperties = {
  fontVariantNumeric: "tabular-nums lining-nums",
};
const DASH = <span className="text-muted-foreground">—</span>;

function signedRatio(value: number | null): { sign: string; color: string } {
  if (value == null) return { sign: "", color: "text-muted-foreground" };
  if (value > 0) return { sign: "+", color: "text-pos" };
  if (value < 0) return { sign: "−", color: "text-neg" };
  return { sign: "", color: "text-foreground" };
}

export type PortfolioStatTilesProps = {
  points: PortfolioTimeseriesPoint[];
  periods?: PeriodPerformance[];
  selectedPeriod: RollingPeriod;
  benchmarkName?: string | null;
  isLoading?: boolean;
  className?: string;
};

export function PortfolioStatTiles({
  points,
  periods,
  selectedPeriod,
  benchmarkName = "Benchmark",
  isLoading = false,
  className,
}: PortfolioStatTilesProps) {
  if (isLoading || points.length === 0) {
    return (
      <div className={cn("grid grid-cols-2 gap-4 lg:grid-cols-4", className)}>
        <Tile label="Current Value">
          <span className={cn(FIGURE, "text-foreground")}>{DASH}</span>
        </Tile>
        <Tile label="Period TWR">
          <span className={cn(FIGURE, "text-foreground")}>{DASH}</span>
        </Tile>
        <Tile label="Money-Weighted XIRR">
          <span className={cn(FIGURE, "text-foreground")}>{DASH}</span>
        </Tile>
        <Tile label="Risk Metrics">
          <span className={cn(FIGURE, "text-foreground")}>{DASH}</span>
        </Tile>
      </div>
    );
  }

  const latestPoint = points[points.length - 1];
  const currentValue = latestPoint.portfolio_value_paise;
  const investedCapital = latestPoint.invested_capital_paise;
  const totalGain = currentValue - investedCapital;
  const totalGainPct =
    investedCapital > 0 ? totalGain / investedCapital : null;

  const gainSign = totalGain > 0 ? "+" : totalGain < 0 ? "−" : "";
  const gainColor =
    totalGain > 0 ? "text-pos" : totalGain < 0 ? "text-neg" : "text-foreground";

  // Selected period performance
  const periodMeta = periods?.find((p) => p.period === selectedPeriod);
  const twrPct = periodMeta?.twr_pct ?? null;
  const twrAnnualized = periodMeta?.twr_annualized ?? null;
  const activeDays = periodMeta?.active_days ?? 0;
  const isMultiYear =
    selectedPeriod === "1Y" ||
    selectedPeriod === "3Y" ||
    selectedPeriod === "5Y" ||
    (selectedPeriod === "all" && activeDays >= 365);

  const twrParts = signedRatio(twrPct);
  const pXirrParts = signedRatio(periodMeta?.xirr ?? null);
  const bXirrParts = signedRatio(periodMeta?.benchmark_xirr ?? null);
  const alphaParts = signedRatio(periodMeta?.alpha ?? null);

  const maxDrawdown = periodMeta?.max_drawdown ?? 0;
  const volatility = periodMeta?.volatility_annualized ?? null;

  return (
    <div className={cn("grid grid-cols-2 gap-4 lg:grid-cols-4", className)}>
      {/* 1. Current Value & Total Unrealized Gain/Loss */}
      <Tile
        label="Current Value"
        sub={
          <div className="flex items-center gap-1.5 font-mono text-[11px]" style={TABULAR}>
            <span className="text-muted-foreground">Gain:</span>
            <span className={gainColor}>
              <Sensitive>
                {gainSign}
                {formatINR(Math.abs(totalGain))}
              </Sensitive>
            </span>
            {totalGainPct != null ? (
              <span className={gainColor}>
                ({gainSign}
                {formatPercent(Math.abs(totalGainPct))})
              </span>
            ) : null}
          </div>
        }
      >
        <span className={cn(FIGURE, "text-foreground")} style={MONO}>
          <Sensitive>{formatINR(currentValue)}</Sensitive>
        </span>
      </Tile>

      {/* 2. Period TWR (% and Annualized CAGR when >= 1Y) */}
      <Tile
        label={`TWR (${selectedPeriod.toUpperCase()})`}
        sub={
          isMultiYear && twrAnnualized != null ? (
            <span className="text-muted-foreground font-mono text-[11px]" style={TABULAR}>
              CAGR:{" "}
              <span className={twrAnnualized >= 0 ? "text-pos" : "text-neg"}>
                {twrAnnualized >= 0 ? "+" : "−"}
                {formatPercent(Math.abs(twrAnnualized))} p.a.
              </span>
            </span>
          ) : (
            <span className="text-muted-foreground text-[11px]">
              {activeDays} active day{activeDays === 1 ? "" : "s"}
            </span>
          )
        }
      >
        <span className={cn(FIGURE, "tabular-nums", twrParts.color)} style={TABULAR}>
          {twrPct != null ? (
            <>
              {twrParts.sign}
              {formatPercent(Math.abs(twrPct))}
            </>
          ) : (
            DASH
          )}
        </span>
      </Tile>

      {/* 3. Money-weighted XIRR vs. Benchmark & Alpha */}
      <Tile
        label="XIRR vs. Benchmark"
        sub={
          periodMeta?.benchmark_xirr != null ? (
            <div className="flex items-center gap-1.5 font-mono text-[11px]" style={TABULAR}>
              <span className="text-muted-foreground">{benchmarkName}:</span>
              <span className={bXirrParts.color}>
                {bXirrParts.sign}
                {formatPercent(Math.abs(periodMeta.benchmark_xirr))}
              </span>
              <span>·</span>
              <span className="text-muted-foreground">α:</span>
              <span className={alphaParts.color}>
                {alphaParts.sign}
                {periodMeta.alpha != null
                  ? `${(periodMeta.alpha * 100).toFixed(2)}%`
                  : "—"}
              </span>
            </div>
          ) : (
            <span className="text-muted-foreground text-[11px]">
              Counterfactual benchmark return
            </span>
          )
        }
      >
        <span className={cn(FIGURE, "tabular-nums", pXirrParts.color)} style={TABULAR}>
          {periodMeta?.xirr != null ? (
            <>
              {pXirrParts.sign}
              {formatPercent(Math.abs(periodMeta.xirr))}
            </>
          ) : (
            DASH
          )}
        </span>
      </Tile>

      {/* 4. Risk Metrics: Max Drawdown & Volatility */}
      <Tile
        label="Max Drawdown"
        sub={
          volatility != null ? (
            <span className="text-muted-foreground font-mono text-[11px]" style={TABULAR}>
              Ann. Volatility: {formatPercent(volatility)}
            </span>
          ) : (
            <span className="text-muted-foreground text-[11px]">
              Annualized standard deviation
            </span>
          )
        }
      >
        <span
          className={cn(
            FIGURE,
            "tabular-nums font-mono",
            maxDrawdown < 0 ? "text-neg" : "text-foreground",
          )}
          style={TABULAR}
        >
          {maxDrawdown !== 0 ? (
            `${(maxDrawdown * 100).toFixed(2)}%`
          ) : (
            "0.00%"
          )}
        </span>
      </Tile>
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
      {sub != null ? <div className="mt-1.5 text-[11.5px] leading-none">{sub}</div> : null}
    </div>
  );
}
