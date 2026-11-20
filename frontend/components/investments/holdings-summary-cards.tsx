"use client";

/**
 * Top summary KPI cards for /holdings (Phase 2).
 *
 * Re-aggregates Total Invested, Current Value, and Total Gain/Loss from the
 * active holdings list, handling domestic INR accounts and US Brokerage (USD)
 * with dual-currency display.
 */

import { Sensitive } from "@/components/balance-visibility";
import { Eyebrow } from "@/components/ui/eyebrow";
import { MONO } from "@/components/ui/table";
import { formatMoney, formatPercent } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { HoldingRead } from "@/lib/api/client";
import type { InvestmentAccountRead } from "@/lib/types/investment-accounts";

const FIGURE = "text-[20px] font-semibold leading-none tracking-[-0.02em]";

export type HoldingsSummaryCardsProps = {
  holdings: HoldingRead[];
  selectedAccount?: InvestmentAccountRead | null;
  isLoading?: boolean;
};

export function HoldingsSummaryCards({
  holdings,
  selectedAccount,
  isLoading = false,
}: HoldingsSummaryCardsProps) {
  const isUsd = selectedAccount?.currency === "USD";

  // Domestic / Rollup sums in INR paise
  const investedInr = holdings.reduce((sum, h) => sum + h.invested_inr_paise, 0);
  const currentValueInr = holdings.reduce(
    (sum, h) => sum + (h.current_value_inr_paise ?? 0),
    0,
  );
  const pnlInr = holdings.reduce(
    (sum, h) => sum + (h.unrealized_pnl_inr_paise ?? 0),
    0,
  );

  // Native sums (relevant when scoped to a USD account, in USD cents)
  const investedNative = holdings.reduce(
    (sum, h) => sum + h.invested_native_paise,
    0,
  );
  const currentValueNative = holdings.reduce(
    (sum, h) => sum + (h.current_value_native_paise ?? 0),
    0,
  );
  const pnlNative = holdings.reduce(
    (sum, h) => sum + (h.unrealized_pnl_native_paise ?? 0),
    0,
  );

  const nullNavCount = holdings.filter((h) => h.current_nav == null).length;
  // When every holding has no price/NAV, value is ₹0/$0 by rule;
  // suppressing a misleading -100% loss matches portfolio-summary.tsx.
  const allUnpriced = holdings.length > 0 && nullNavCount === holdings.length;

  // Percentage return
  const activeInvested = isUsd ? investedNative : investedInr;
  const activePnl = isUsd ? pnlNative : pnlInr;
  const returnPct =
    activeInvested > 0 && !allUnpriced ? activePnl / activeInvested : null;

  const inrSign = pnlInr > 0 ? "+" : pnlInr < 0 ? "−" : "";
  const nativeSign = pnlNative > 0 ? "+" : pnlNative < 0 ? "−" : "";
  const pnlColor =
    allUnpriced
      ? "text-foreground"
      : activePnl > 0
        ? "text-pos"
        : activePnl < 0
          ? "text-neg"
          : "text-foreground";

  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
      {/* 1. Current Value */}
      <div className="flex flex-col justify-between rounded-lg border border-border bg-card p-4">
        <Eyebrow>Current Value</Eyebrow>
        <div className="mt-2">
          <span className={cn(FIGURE, "text-foreground")} style={MONO}>
            {isLoading ? (
              "—"
            ) : isUsd ? (
              <Sensitive>{formatMoney(currentValueNative, "USD")}</Sensitive>
            ) : (
              <Sensitive>{formatMoney(currentValueInr, "INR")}</Sensitive>
            )}
          </span>
        </div>
        <div className="mt-1 text-[11.5px] leading-tight text-muted-foreground flex items-center gap-1.5 flex-wrap">
          {isUsd && !isLoading ? (
            <span>
              ≈ <Sensitive>{formatMoney(currentValueInr, "INR")}</Sensitive>
            </span>
          ) : null}
          {nullNavCount > 0 ? (
            <span>
              {isUsd && !isLoading ? "· " : ""}
              {nullNavCount} unpriced ({isUsd ? "$0" : "₹0"})
            </span>
          ) : (
            <span>
              {holdings.length} holding{holdings.length === 1 ? "" : "s"}
            </span>
          )}
        </div>
      </div>

      {/* 2. Total Invested */}
      <div className="flex flex-col justify-between rounded-lg border border-border bg-card p-4">
        <Eyebrow>Total Invested</Eyebrow>
        <div className="mt-2">
          <span className={cn(FIGURE, "text-foreground")} style={MONO}>
            {isLoading ? (
              "—"
            ) : isUsd ? (
              <Sensitive>{formatMoney(investedNative, "USD")}</Sensitive>
            ) : (
              <Sensitive>{formatMoney(investedInr, "INR")}</Sensitive>
            )}
          </span>
        </div>
        <div className="mt-1 text-[11.5px] leading-tight text-muted-foreground">
          {isUsd && !isLoading ? (
            <span>
              ≈ <Sensitive>{formatMoney(investedInr, "INR")}</Sensitive>
            </span>
          ) : (
            <span>Cost basis</span>
          )}
        </div>
      </div>

      {/* 3. Total Gain/Loss */}
      <div className="flex flex-col justify-between rounded-lg border border-border bg-card p-4">
        <Eyebrow>Total Gain/Loss</Eyebrow>
        <div className="mt-2 flex items-baseline gap-2">
          <span className={cn(FIGURE, pnlColor)} style={MONO}>
            {isLoading || allUnpriced ? (
              "—"
            ) : isUsd ? (
              <Sensitive>
                {nativeSign}
                {formatMoney(Math.abs(pnlNative), "USD")}
              </Sensitive>
            ) : (
              <Sensitive>
                {inrSign}
                {formatMoney(Math.abs(pnlInr), "INR")}
              </Sensitive>
            )}
          </span>
          {returnPct != null && !isLoading ? (
            <span
              className={cn("text-[13px] font-medium", pnlColor)}
              style={MONO}
            >
              ({activePnl > 0 ? "+" : activePnl < 0 ? "−" : ""}
              {formatPercent(Math.abs(returnPct))})
            </span>
          ) : null}
        </div>
        <div className="mt-1 text-[11.5px] leading-tight text-muted-foreground">
          {isUsd && !isLoading ? (
            <span>
              ≈ <Sensitive>{inrSign}{formatMoney(Math.abs(pnlInr), "INR")}</Sensitive>
            </span>
          ) : (
            <span>Unrealized P&amp;L</span>
          )}
        </div>
      </div>
    </div>
  );
}
