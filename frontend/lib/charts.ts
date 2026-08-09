/** Shared chart-axis helpers for the /spending time-series bars (spend-by-period,
 * cashflow, category-trend). Money formatting lives in `lib/format.ts`
 * (`compactINR`) and window math in `lib/dates.ts` (`trailingMonths`,
 * `trailingWeeksWindow`) — this module is just the period → x-axis-label mapping. */

/** Grain of a time-series bucket: ISO week or calendar month. */
export type Grain = "week" | "month";

/** 1-indexed month abbreviations (index 0 unused) for compact x-axis labels. */
export const MONTH_ABBR = [
  "",
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

/** "2026-W23" → "W23"; "2026-06" → "Jun". Compact x-axis tick for a bucket
 * period label (the `SpendByPeriodBucket.period` / `CashflowByPeriodBucket.period`
 * shape). Falls back to the raw period string if it doesn't parse. */
export function periodLabel(period: string, grain: Grain): string {
  if (grain === "week") {
    const w = period.split("-W")[1];
    return w ? `W${w}` : period;
  }
  const m = Number(period.split("-")[1]);
  return MONTH_ABBR[m] ?? period;
}
