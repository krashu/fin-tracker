"use client";

/**
 * Current investment positions — the data-driven half of /holdings. Read-only:
 * the backend's holdings_service replays every investment transaction through a
 * FIFO lot queue and returns net units, remaining cost basis, current value, and
 * unrealized P&L per instrument. Rendered as received — no client recompute.
 *
 * Two derived columns are added client-side:
 *   • %Alloc — this holding's current value over the priced-holdings total
 *     (NAV-bearing only, per the cross-part null-NAV rule). Both sides use the
 *     INR rollup (`current_value_inr_paise`), never native paise — an INR and a
 *     USD holding must not be summed 1:1 (the cent↔paise bug).
 *   • XIRR — money-weighted return from GET /portfolio/summary, merged by
 *     instrument_id. "—" until that query resolves, or when unsolvable / null-NAV.
 *
 * Current value carries the age of the price behind it (`nav_staleness_days`, computed
 * server-side), amber past `STALENESS_WARN_DAYS`. Without it the table showed a number
 * off a 90-day-old hand-typed NAV exactly as confidently as one refreshed this morning.
 *
 * Per-row money columns stay in the instrument's own currency (₹ / $) via
 * `formatMoney(x, currency)` — only the cross-row %Alloc aggregation rolls up to INR.
 */
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";

import {
  getPortfolioSummary,
  listHoldings,
  type HoldingRead,
} from "@/lib/api/client";
import {
  formatDecimalMoney,
  formatMoney,
  formatPercent,
  formatUnits,
} from "@/lib/format";
import {
  ASSET_CLASS_LABELS,
  MANUALLY_PRICED_CLASSES,
  STALENESS_WARN_DAYS,
} from "@/lib/investments";
import { cn } from "@/lib/utils";
import { Sensitive } from "@/components/balance-visibility";
import { MONO, StateRow, Td, Th } from "@/components/ui/table";

const COLS = 9;

export function HoldingsTable() {
  const query = useQuery({ queryKey: ["holdings"], queryFn: listHoldings });
  // Per-holding XIRR rides on the portfolio summary (shared cache key, so the
  // /portfolio page and this table fetch it once). It degrades independently of
  // the holdings query — a missing/loading/errored summary just shows "—".
  const summary = useQuery({
    queryKey: ["portfolio", "summary"],
    queryFn: getPortfolioSummary,
  });

  const rows = query.data?.holdings ?? [];
  // %Alloc denominator: total current value across PRICED holdings, in INR
  // (null-NAV — and USD holdings with no cached FX rate — contribute neither
  // value nor a denominator share). INR rollup, never native, so mixed-currency
  // shares are comparable.
  const pricedTotal = rows.reduce(
    (s, h) => s + (h.current_value_inr_paise ?? 0),
    0,
  );
  const xirrByInstrument = new Map(
    (summary.data?.holding_xirr ?? []).map((x) => [x.instrument_id, x.xirr]),
  );

  return (
    <section className="mt-6 rounded-lg border border-border bg-card">
      {/* Wide 9-column table: scroll horizontally inside the card instead of
          letting it overflow the page (the body must never scroll horizontally).
          Short list, so a non-sticky header here is a non-issue. */}
      <div className="overflow-x-auto">
        <table className="w-full min-w-[1040px] border-separate border-spacing-0">
          <colgroup>
            <col />
            <col style={{ width: 120 }} />
            <col style={{ width: 120 }} />
            <col style={{ width: 130 }} />
            <col style={{ width: 140 }} />
            <col style={{ width: 150 }} />
            <col style={{ width: 150 }} />
            <col style={{ width: 80 }} />
            <col style={{ width: 100 }} />
          </colgroup>
          <thead>
            <tr>
              <Th first>Instrument</Th>
              <Th>Asset class</Th>
              <Th align="right">Net units</Th>
              <Th align="right">Avg cost</Th>
              <Th align="right">Invested</Th>
              <Th align="right">Current value</Th>
              <Th align="right">Unrealized P&amp;L</Th>
              <Th align="right">%Alloc</Th>
              <Th align="right" last>
                XIRR
              </Th>
            </tr>
          </thead>
          <tbody>
            {query.status === "pending" ? (
              <StateRow colSpan={COLS}>Loading…</StateRow>
            ) : query.status === "error" ? (
              <StateRow colSpan={COLS} tone="error">
                Couldn’t load holdings — is the API running?
              </StateRow>
            ) : rows.length === 0 ? (
              <StateRow colSpan={COLS}>
                No holdings yet — record a buy on{" "}
                <Link
                  href="/investments"
                  className="font-medium text-primary hover:underline"
                >
                  Transactions
                </Link>
                .
              </StateRow>
            ) : (
              rows.map((h, i) => (
                <HoldingRow
                  key={h.instrument_id}
                  last={i === rows.length - 1}
                  holding={h}
                  pricedTotal={pricedTotal}
                  xirr={xirrByInstrument.get(h.instrument_id) ?? null}
                />
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function HoldingRow({
  last,
  holding,
  pricedTotal,
  xirr,
}: {
  last: boolean;
  holding: HoldingRead;
  pricedTotal: number;
  xirr: number | null;
}) {
  const border = last ? "" : "border-b border-border/70";
  const c = holding.currency;
  const pnl = holding.unrealized_pnl_native_paise;
  const sign = pnl == null ? "" : pnl > 0 ? "+" : pnl < 0 ? "−" : "";

  // %Alloc uses the INR rollup for this row and the denominator alike (never
  // native — see file docstring). "—" when the priced total is 0 (all-null-NAV /
  // empty) or this row is itself unpriced / FX-unavailable; never divide by zero.
  const cvInr = holding.current_value_inr_paise;
  const allocPct =
    pricedTotal > 0 && cvInr != null ? cvInr / pricedTotal : null;

  // Valuation age of the price behind Current value. Server-computed (a raw
  // nav_updated_at serializes without a timezone suffix, so `new Date()` would read it as
  // local time) — the only job here is the threshold comparison.
  const age = holding.nav_staleness_days;
  const stale = age != null && age >= STALENESS_WARN_DAYS;
  // For these classes refresh-navs has no source and returns `skipped`, so "refresh
  // prices" is unactionable advice — the user is the price source.
  const manuallyPriced = MANUALLY_PRICED_CLASSES.has(holding.asset_class);

  // xirr is null whenever the summary hasn't resolved or the value is unsolvable
  // — both render "—" (no sign, muted).
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
    <tr>
      <Td first borderClass={border}>
        <div className="flex min-w-0 flex-col justify-center gap-0.5 min-h-[32px]">
          <span
            className="truncate text-[13px] font-medium text-foreground"
            style={{ letterSpacing: "-0.005em" }}
          >
            {holding.symbol}
          </span>
          <span
            className="truncate text-[11.5px] text-muted-foreground"
            style={{ letterSpacing: "-0.003em" }}
            title={holding.name}
          >
            {holding.name}
          </span>
        </div>
      </Td>
      <Td borderClass={border}>
        <span
          className="text-[12.5px] text-foreground/80"
          style={{ letterSpacing: "-0.003em" }}
        >
          {ASSET_CLASS_LABELS[holding.asset_class]}
        </span>
      </Td>
      <Td align="right" borderClass={border}>
        <span className="text-[12.5px] text-foreground/80" style={MONO}>
          <Sensitive>{formatUnits(holding.net_units)}</Sensitive>
        </span>
      </Td>
      <Td align="right" borderClass={border}>
        <span className="text-[12.5px] text-foreground/80" style={MONO}>
          <Sensitive>
            {formatDecimalMoney(holding.avg_cost_native, c)}
          </Sensitive>
        </span>
      </Td>
      <Td align="right" borderClass={border}>
        <span className="text-[12.5px] text-foreground/80" style={MONO}>
          <Sensitive>{formatMoney(holding.invested_native_paise, c)}</Sensitive>
        </span>
      </Td>
      <Td align="right" borderClass={border}>
        <div className="flex flex-col items-end gap-0.5">
          <span className="text-[13px] font-medium text-foreground" style={MONO}>
            {holding.current_value_native_paise == null ? (
              "—"
            ) : (
              <Sensitive>
                {formatMoney(holding.current_value_native_paise, c)}
              </Sensitive>
            )}
          </span>
          {/* How old the price above is. Not wrapped in <Sensitive>: an age is not an
              amount, and hiding it would hide the caveat rather than the balance. */}
          {age == null ? null : (
            <span
              className={cn(
                "text-[11px] leading-none",
                stale ? "text-amber-600 dark:text-amber-500" : "text-muted-foreground",
              )}
              title={
                stale
                  ? `This price is ${age} days old. ${manuallyPriced ? "This asset class has no price source — update it yourself." : "Refresh prices to update it."}`
                  : undefined
              }
            >
              {age === 0 ? "priced today" : `${age}d old`}
            </span>
          )}
        </div>
      </Td>
      <Td align="right" borderClass={border}>
        <span
          className={cn(
            "text-[13px] font-medium",
            pnl == null
              ? "text-muted-foreground"
              : pnl > 0
                ? "text-pos"
                : pnl < 0
                  ? "text-neg"
                  : "text-foreground",
          )}
          style={MONO}
        >
          {pnl == null ? (
            "—"
          ) : (
            <Sensitive>{`${sign}${formatMoney(Math.abs(pnl), c)}`}</Sensitive>
          )}
        </span>
      </Td>
      <Td align="right" borderClass={border}>
        <span className="text-[12.5px] text-foreground/80" style={MONO}>
          {allocPct == null ? "—" : formatPercent(allocPct)}
        </span>
      </Td>
      <Td align="right" last borderClass={border}>
        <span
          className={cn("text-[12.5px] font-medium", xirrColor)}
          style={MONO}
        >
          {xirr == null ? "—" : `${xirrSign}${formatPercent(Math.abs(xirr))}`}
        </span>
      </Td>
    </tr>
  );
}
