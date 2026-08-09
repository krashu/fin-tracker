"use client";

/**
 * Live investment-transaction log — the data-driven half of /investments. The
 * page shell owns the heading; this client island fetches and renders.
 * InvestmentTransactionRead is flat (instrument_id only), so the symbol/name and
 * native currency come from an instruments lookup.
 *
 * Slice 1 is a plain full list (server order: newest-first), no filters /
 * pagination / selection — those land if a real consumer asks (CLAUDE.md §2).
 *
 * A row carrying `pair_id` is one leg of ONE economic event (today: an IDCW
 * reinvestment's `dividend` + `buy` couple) and is badged as such. The two legs stay
 * SEPARATE rows deliberately — this is a transaction log, not a summary, and the legs
 * have genuinely different FIFO meaning (the `buy` opens its own lot with its own cost
 * basis and acquisition date; the `dividend` opens nothing). Merging them would
 * re-introduce exactly the conflation the pair exists to prevent.
 */
import { useQuery } from "@tanstack/react-query";

import {
  listInstruments,
  listInvestmentTransactions,
  type InstrumentRead,
} from "@/lib/api/client";
import {
  formatDate,
  formatDecimalMoney,
  formatMoney,
  formatUnits,
} from "@/lib/format";
import { INVESTMENT_TYPE_LABELS } from "@/lib/investments";
import { Sensitive } from "@/components/balance-visibility";
import { Badge } from "@/components/ui/badge";
import { MONO, StateRow, Td, Th } from "@/components/ui/table";

const COLS = 7;

export function InvestmentsBoard() {
  const txnsQuery = useQuery({
    queryKey: ["investment-transactions"],
    queryFn: listInvestmentTransactions,
  });
  const instrumentsQuery = useQuery({
    queryKey: ["instruments"],
    queryFn: listInstruments,
  });

  const instrumentsById = new Map<number, InstrumentRead>(
    (instrumentsQuery.data ?? []).map((i) => [i.id, i]),
  );
  const rows = txnsQuery.data ?? [];

  return (
    <section className="mt-6 rounded-lg border border-border bg-card">
      {/* Wide table: scroll horizontally inside the card instead of overflowing
          the page. Short list, so a non-sticky header here is a non-issue. */}
      <div className="overflow-x-auto">
        <table className="w-full min-w-[860px] border-separate border-spacing-0">
          <colgroup>
            <col style={{ width: 92 }} />
            <col />
            <col style={{ width: 96 }} />
            <col style={{ width: 130 }} />
            <col style={{ width: 140 }} />
            <col style={{ width: 150 }} />
            <col style={{ width: 120 }} />
          </colgroup>
          <thead>
            <tr>
              <Th first>Date</Th>
              <Th>Instrument</Th>
              <Th>Type</Th>
              <Th align="right">Units</Th>
              <Th align="right">Price</Th>
              <Th align="right">Amount</Th>
              <Th align="right" last>
                Fees
              </Th>
            </tr>
          </thead>
          <tbody>
            {txnsQuery.status === "pending" ? (
              <StateRow colSpan={COLS}>Loading…</StateRow>
            ) : txnsQuery.status === "error" ? (
              <StateRow colSpan={COLS} tone="error">
                Couldn’t load transactions — is the API running?
              </StateRow>
            ) : rows.length === 0 ? (
              <StateRow colSpan={COLS}>
                {instrumentsQuery.data?.length === 0
                  ? "No instruments yet — add an instrument first, then record buys and sells against it."
                  : "No investment transactions yet — add one to get started."}
              </StateRow>
            ) : (
              rows.map((t, i) => {
                const instrument = instrumentsById.get(t.instrument_id);
                const currency = instrument?.currency ?? "INR";
                return (
                  <TxnRow
                    key={t.id}
                    last={i === rows.length - 1}
                    date={t.date}
                    symbol={instrument?.symbol ?? "—"}
                    name={instrument?.name ?? null}
                    typeLabel={INVESTMENT_TYPE_LABELS[t.transaction_type]}
                    pairId={t.pair_id}
                    units={t.units}
                    price={t.price_per_unit_native}
                    amountPaise={t.amount_native_paise}
                    feesPaise={t.fees_native_paise}
                    currency={currency}
                  />
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function TxnRow({
  last,
  date,
  symbol,
  name,
  typeLabel,
  pairId,
  units,
  price,
  amountPaise,
  feesPaise,
  currency,
}: {
  last: boolean;
  date: string;
  symbol: string;
  name: string | null;
  typeLabel: string;
  pairId: number | null;
  units: string;
  price: string | null;
  amountPaise: number;
  feesPaise: number;
  currency: "INR" | "USD";
}) {
  const border = last ? "" : "border-b border-border/70";
  return (
    <tr>
      <Td first borderClass={border}>
        <span
          className="text-[12.5px] tabular-nums text-foreground/80"
          style={{ fontVariantNumeric: "tabular-nums lining-nums" }}
        >
          {formatDate(date)}
        </span>
      </Td>
      <Td borderClass={border}>
        <div className="flex min-w-0 flex-col justify-center gap-0.5 min-h-[32px]">
          <span
            className="truncate text-[13px] font-medium text-foreground"
            style={{ letterSpacing: "-0.005em" }}
          >
            {symbol}
          </span>
          {name ? (
            <span
              className="truncate text-[11.5px] text-muted-foreground"
              style={{ letterSpacing: "-0.003em" }}
              title={name}
            >
              {name}
            </span>
          ) : null}
        </div>
      </Td>
      <Td borderClass={border}>
        <div className="flex items-center gap-1.5">
          <span
            className="text-[12.5px] text-foreground/80"
            style={{ letterSpacing: "-0.003em" }}
          >
            {typeLabel}
          </span>
          {pairId != null ? (
            // Order-independent on purpose: the two legs happen to render adjacent
            // (server order is date desc, id desc, and they get consecutive ids on one
            // date), but the badge carries the meaning either way.
            <Badge
              variant="outline"
              size="sm"
              title={`Linked to transaction #${pairId} — two legs of one IDCW reinvestment`}
            >
              ↔ paired
            </Badge>
          ) : null}
        </div>
      </Td>
      <Td align="right" borderClass={border}>
        <span className="text-[12.5px] text-foreground/80" style={MONO}>
          {parseFloat(units) === 0 ? (
            "—"
          ) : (
            <Sensitive>{formatUnits(units)}</Sensitive>
          )}
        </span>
      </Td>
      <Td align="right" borderClass={border}>
        <span className="text-[12.5px] text-foreground/80" style={MONO}>
          {price == null ? (
            "—"
          ) : (
            <Sensitive>{formatDecimalMoney(price, currency)}</Sensitive>
          )}
        </span>
      </Td>
      <Td align="right" borderClass={border}>
        <span className="text-[13px] font-medium text-foreground" style={MONO}>
          <Sensitive>{formatMoney(amountPaise, currency)}</Sensitive>
        </span>
      </Td>
      <Td align="right" last borderClass={border}>
        <span className="text-[12.5px] text-muted-foreground" style={MONO}>
          {feesPaise === 0 ? (
            "—"
          ) : (
            <Sensitive>{formatMoney(feesPaise, currency)}</Sensitive>
          )}
        </span>
      </Td>
    </tr>
  );
}
