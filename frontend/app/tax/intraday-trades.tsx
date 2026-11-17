"use client";

/**
 * Intraday trades for one tax year (PRD §F11, ADR-0015 rules 7-8): same-day buy/sell
 * matches, speculative business income listed apart from capital gains. Rendered as
 * received from `GET /tax/intraday-trades` - the profit is server-computed, never
 * recomputed here. Every amount is INR (indian_* instruments are INR at every write
 * boundary), so there is no currency column.
 */
import { useQuery } from "@tanstack/react-query";

import { listIntradayTrades } from "@/lib/api/client";
import { formatDateWithYear, formatINR, formatUnits } from "@/lib/format";
import { taxYearLabel } from "@/lib/period";
import { cn } from "@/lib/utils";
import { Sensitive } from "@/components/balance-visibility";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { MONO, StateRow, Td, Th } from "@/components/ui/table";

const COLS = 7;

function toneClass(paise: number): string {
  return paise > 0 ? "text-pos" : paise < 0 ? "text-neg" : "text-foreground";
}

export function IntradayTrades({ year }: { year: number }) {
  const query = useQuery({
    queryKey: ["tax", "intraday-trades", year],
    queryFn: () => listIntradayTrades(year),
  });
  const trades = query.data?.trades ?? [];

  return (
    <Card>
      <CardHeader>
        <CardTitle>Intraday trades</CardTitle>
        <p className="text-[12.5px] text-muted-foreground">
          Speculative business income: listed apart from capital gains and never added to
          them. The tax on it is not computed here.
        </p>
      </CardHeader>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[860px] border-separate border-spacing-0">
          <thead>
            <tr>
              <Th first>Date</Th>
              <Th>Instrument</Th>
              <Th align="right">Units</Th>
              <Th align="right">Bought for</Th>
              <Th align="right">Sold for</Th>
              <Th align="right">Sell charges</Th>
              <Th align="right" last>
                Profit / loss
              </Th>
            </tr>
          </thead>
          <tbody>
            {query.status === "pending" ? (
              <StateRow colSpan={COLS}>Loading…</StateRow>
            ) : query.status === "error" ? (
              <StateRow colSpan={COLS} tone="error">
                Couldn’t load intraday trades.
              </StateRow>
            ) : trades.length === 0 ? (
              <StateRow colSpan={COLS}>
                No intraday trades in {taxYearLabel(year)}.
              </StateRow>
            ) : (
              <>
                {trades.map((t) => (
                  <tr key={`${t.sell_txn_id}-${t.buy_txn_id}`}>
                    <Td first borderClass="border-b border-border/70">
                      <span className="text-[12.5px] text-foreground/80">
                        {formatDateWithYear(t.date)}
                      </span>
                    </Td>
                    <Td borderClass="border-b border-border/70">
                      <div className="flex min-w-0 flex-col gap-0.5">
                        <span className="truncate text-[13px] font-medium text-foreground">
                          {t.symbol}
                        </span>
                        <span
                          className="truncate text-[11.5px] text-muted-foreground"
                          title={t.name}
                        >
                          {t.name}
                        </span>
                      </div>
                    </Td>
                    <Td align="right" borderClass="border-b border-border/70">
                      <span className="text-[12.5px] text-foreground/80" style={MONO}>
                        <Sensitive>{formatUnits(t.units)}</Sensitive>
                      </span>
                    </Td>
                    <Td align="right" borderClass="border-b border-border/70">
                      <span className="text-[12.5px] text-foreground/80" style={MONO}>
                        <Sensitive>{formatINR(t.buy_value_paise)}</Sensitive>
                      </span>
                    </Td>
                    <Td align="right" borderClass="border-b border-border/70">
                      <span className="text-[12.5px] text-foreground/80" style={MONO}>
                        <Sensitive>{formatINR(t.sell_value_paise)}</Sensitive>
                      </span>
                    </Td>
                    <Td align="right" borderClass="border-b border-border/70">
                      <span className="text-[12.5px] text-foreground/80" style={MONO}>
                        <Sensitive>{formatINR(t.sell_charges_paise)}</Sensitive>
                      </span>
                    </Td>
                    <Td align="right" last borderClass="border-b border-border/70">
                      <span
                        className={cn("text-[13px] font-medium", toneClass(t.profit_paise))}
                        style={MONO}
                      >
                        <Sensitive>{formatINR(t.profit_paise)}</Sensitive>
                      </span>
                    </Td>
                  </tr>
                ))}
                <tr>
                  <Td first borderClass="">
                    <span className="text-[13px] font-medium text-foreground">Total</span>
                  </Td>
                  <Td borderClass="">{null}</Td>
                  <Td borderClass="">{null}</Td>
                  <Td borderClass="">{null}</Td>
                  <Td borderClass="">{null}</Td>
                  <Td borderClass="">{null}</Td>
                  <Td align="right" last borderClass="">
                    <span
                      className={cn(
                        "text-[13px] font-medium",
                        toneClass(query.data?.total_profit_paise ?? 0),
                      )}
                      style={MONO}
                    >
                      <Sensitive>{formatINR(query.data?.total_profit_paise ?? 0)}</Sensitive>
                    </span>
                  </Td>
                </tr>
              </>
            )}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
