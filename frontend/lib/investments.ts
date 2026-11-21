import type {
  AssetClass,
  Exchange,
  InstrumentRead,
  InvestmentTransactionType,
} from "@/lib/api/client";
import type {
  PortfolioTimeseriesPoint,
  PeriodPerformance,
} from "@/lib/api/portfolio-timeseries";

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

export const ROLLING_PERIODS = [
  "1M",
  "3M",
  "6M",
  "1Y",
  "3Y",
  "5Y",
  "all",
] as const;

export type RollingPeriod = (typeof ROLLING_PERIODS)[number];

export const ROLLING_PERIOD_LABELS: Record<RollingPeriod, string> = {
  "1M": "1M",
  "3M": "3M",
  "6M": "6M",
  "1Y": "1Y",
  "3Y": "3Y",
  "5Y": "5Y",
  all: "All",
};

export type RebasedTimeseriesPoint = {
  date: string;
  portfolioReturn: number;
  benchmarkReturn: number | null;
  portfolioValuePaise: number;
  investedCapitalPaise: number;
  benchmarkValuePaise: number | null;
  dailyReturn: number;
  isInvested: boolean;
};

export function rebaseTimeseriesPoints(
  points: PortfolioTimeseriesPoint[],
  mode: "pct" | "base100" = "pct",
): RebasedTimeseriesPoint[] {
  if (points.length === 0) return [];

  const baseTwrMicros = points[0].twr_index_micros;
  let cumBenchmarkFactor = 1.0;

  return points.map((p, idx) => {
    const pFactor =
      baseTwrMicros > 0 ? p.twr_index_micros / baseTwrMicros : 1.0;
    const portfolioReturn =
      mode === "pct" ? (pFactor - 1.0) * 100 : pFactor * 100;

    let benchmarkReturn: number | null = null;
    if (idx > 0 && p.benchmark_value_paise != null) {
      const prevB = points[idx - 1].benchmark_value_paise;
      const currB = p.benchmark_value_paise;
      const cashflow = p.net_cashflow_paise;
      if (prevB != null && prevB > 0) {
        const stepFactor = Math.max(0, (currB - cashflow) / prevB);
        cumBenchmarkFactor *= stepFactor;
      }
    }

    if (p.benchmark_value_paise != null) {
      benchmarkReturn =
        mode === "pct"
          ? (cumBenchmarkFactor - 1.0) * 100
          : cumBenchmarkFactor * 100;
    }

    return {
      date: p.date,
      portfolioReturn: Math.round(portfolioReturn * 100) / 100,
      benchmarkReturn:
        benchmarkReturn != null
          ? Math.round(benchmarkReturn * 100) / 100
          : null,
      portfolioValuePaise: p.portfolio_value_paise,
      investedCapitalPaise: p.invested_capital_paise,
      benchmarkValuePaise: p.benchmark_value_paise,
      dailyReturn: p.daily_return,
      isInvested: p.is_invested,
    };
  });
}

export function filterPointsByPeriod(
  points: PortfolioTimeseriesPoint[],
  period: RollingPeriod,
  periods?: PeriodPerformance[],
): PortfolioTimeseriesPoint[] {
  if (points.length === 0) return [];
  if (period === "all") return points;

  const match = periods?.find((p) => p.period === period);
  if (match?.start_date) {
    const filtered = points.filter((pt) => pt.date >= match.start_date);
    return filtered.length > 0 ? filtered : points;
  }

  const lastPoint = points[points.length - 1];
  const lastDate = new Date(lastPoint.date);
  const targetDate = new Date(lastDate);

  if (period === "1M") targetDate.setMonth(targetDate.getMonth() - 1);
  else if (period === "3M") targetDate.setMonth(targetDate.getMonth() - 3);
  else if (period === "6M") targetDate.setMonth(targetDate.getMonth() - 6);
  else if (period === "1Y") targetDate.setFullYear(targetDate.getFullYear() - 1);
  else if (period === "3Y") targetDate.setFullYear(targetDate.getFullYear() - 3);
  else if (period === "5Y") targetDate.setFullYear(targetDate.getFullYear() - 5);

  const targetDateStr = targetDate.toISOString().slice(0, 10);
  const filtered = points.filter((pt) => pt.date >= targetDateStr);
  return filtered.length > 0 ? filtered : points;
}
