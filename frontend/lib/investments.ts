import type {
  AssetClass,
  Exchange,
  InstrumentRead,
  InvestmentTransactionType,
} from "@/lib/api/client";

/** Warn that a valuation is stale at or beyond this many CALENDAR days.
 *
 * Mirrors `STALENESS_WARN_DAYS` in `backend/app/schemas/performance.py`, which is where
 * the calendar is DEFINED — read it there rather than re-deriving the reasoning here.
 * The short version: a weekend plus one business day, because Friday→Monday is 3 calendar
 * days and the old `>= 3` gate therefore fired every Monday, on the very case its comment
 * claimed to exclude. Re-deriving that comment per call site is what produced the
 * off-by-one, so this constant has exactly one definition on each side of the wire and
 * every consumer compares against it.
 *
 * Applies to both `nav_staleness_days` (portfolio and per-holding) and
 * `fx_staleness_days` — a USD holding valued off a weeks-old rate is computable, but the
 * rate has moved. */
export const STALENESS_WARN_DAYS = 4;

/** Display label for an instrument: "SYMBOL — Name". */
export function instrumentLabel(i: InstrumentRead): string {
  return `${i.symbol} — ${i.name}`;
}

export const ASSET_CLASS_LABELS: Record<AssetClass, string> = {
  indian_equity: "Indian equity",
  indian_mf: "Indian MF",
  us_equity: "US equity",
  us_etf: "US ETF",
  fd: "Fixed deposit",
  bond: "Bond",
  nps: "NPS",
  gold: "Gold",
  other: "Other",
};

/** Asset classes with no automatic price source: `refresh-navs` counts them as `skipped`
 * and a sync press cannot change their NAV however old it is — the user is the price
 * source. Mirrors the "everything else" branch of `nav_snapshot_service`'s routing
 * (`_MF_CLASS` + `_QUOTE_CLASSES` are the ones that DO have a source). Used to keep
 * staleness advice actionable: telling someone to refresh an FD is telling them to press
 * a button that does nothing. */
export const MANUALLY_PRICED_CLASSES: ReadonlySet<AssetClass> = new Set([
  "fd",
  "bond",
  "nps",
  "gold",
  "other",
]);

export const EXCHANGE_LABELS: Record<Exchange, string> = {
  NSE: "NSE",
  BSE: "BSE",
  MFCentral: "MF Central",
  NASDAQ: "NASDAQ",
  NYSE: "NYSE",
  OTHER: "Other",
};

export const INVESTMENT_TYPE_LABELS: Record<InvestmentTransactionType, string> =
  {
    buy: "Buy",
    sell: "Sell",
    sip: "SIP",
    dividend: "Dividend",
    bonus: "Bonus",
    split: "Split",
    switch_in: "Switch in",
    switch_out: "Switch out",
  };

/** The transaction types offered in manual entry this slice. `split` and
 * `switch_*` are CAS-era (server-side / importer-only) and excluded — the
 * backend rejects them on the manual POST. */
export const MANUAL_ENTRY_TYPES: InvestmentTransactionType[] = [
  "buy",
  "sip",
  "sell",
  "dividend",
  "bonus",
];

/** What the entry form's Type control offers — the wire types plus `"reinvestment"`.
 *
 * A reinvestment is NOT a sixth `InvestmentTransactionType`: it is a *pair* of two
 * existing types (`dividend` + `buy`) written by its own endpoint. Keeping it in a
 * separate union is what lets `MANUAL_ENTRY_TYPES` and `INVESTMENT_TYPE_LABELS` above
 * stay honest maps of the wire enum — the board reads labels off the enum, and a
 * pseudo-member would leak into it.
 *
 * It earns a dropdown slot anyway because that is how an AMC statement presents it:
 * one "IDCW Reinvestment" line carrying units, NAV, and amount together. */
export type EntryMode = InvestmentTransactionType | "reinvestment";

export const ENTRY_MODES: EntryMode[] = [...MANUAL_ENTRY_TYPES, "reinvestment"];

export const ENTRY_MODE_LABELS: Record<EntryMode, string> = {
  ...INVESTMENT_TYPE_LABELS,
  reinvestment: "IDCW reinvestment",
};
