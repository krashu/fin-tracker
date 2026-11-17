/**
 * A period is either a calendar month or a calendar year (PRD §F8) — the same
 * month-or-year period the backend's `PeriodWindow` accepts
 * (`app/api/v1/dashboards.py`). Only the tax pages use the Indian financial year
 * (`fyOf`, `currentFy`, `taxYearLabel`, PRD §F11). One value type + four helpers, replacing what used to be
 * `selectedYear` + `selectedMonth` hand-synced separately in three
 * components (overview.tsx, spending-dashboard.tsx, expenses-board.tsx),
 * each re-deriving its own copy of the query-key/query-fn param ternary, the
 * display label, and the `[start, end]` range.
 *
 * Sits beside `lib/dates.ts` (window math) and `lib/categories.ts` (category
 * value semantics) — the same small-value-module idiom (frontend/CLAUDE.md
 * §Charts already pins period/window math to `lib/dates.ts`).
 */

/** `mon` absent = the calendar year `year`; present (1-12) = that calendar month
 * of `year`. */
export type Period = { year: number; mon?: number };

/** The financial year a period falls in, by START year (tax only, PRD §F11): a
 * year period names its own FY; a month is in the FY that began the April on or
 * before it, so January–March belong to the FY that started the previous calendar
 * year. */
export function fyOf(p: Period): number {
  if (p.mon == null) return p.year;
  return p.mon >= 4 ? p.year : p.year - 1;
}

/** The FY containing `now` (the local date), by start year. */
export function currentFy(now: Date = new Date()): number {
  return fyOf({ year: now.getFullYear(), mon: now.getMonth() + 1 });
}

/** The calendar year containing `now` (the local date) — the spend-side year. */
export function currentYear(now: Date = new Date()): number {
  return now.getFullYear();
}

/** The period just before `p`, for a "vs previous" comparison: the previous
 * calendar month (January gives December of the year before), or the previous
 * year. */
export function previousPeriod(p: Period): Period {
  if (p.mon == null) return { year: p.year - 1 };
  return p.mon === 1 ? { year: p.year - 1, mon: 12 } : { year: p.year, mon: p.mon - 1 };
}

/** The canonical wire key — `"YYYY-MM"` for a month, `"YYYY"` for a year.
 * Matches the backend's `PeriodWindow.key` and the `period` field every
 * dashboards response now echoes (stale-cache verification). */
export function periodKey(p: Period): string {
  return p.mon != null ? `${p.year}-${String(p.mon).padStart(2, "0")}` : String(p.year);
}

/** Human display label — `"June 2026"` for a month, `"2026"` for a year. */
export function periodLabel(p: Period): string {
  if (p.mon == null) return String(p.year);
  const d = new Date(Date.UTC(p.year, p.mon - 1, 1));
  return d.toLocaleDateString("en-US", {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  });
}

/** Display label for a tax year (PRD §F11): "Tax year 2026-27" from FY 2026-27 on, when the
 * Income-tax Act, 2025 names the FY the tax year; "FY 2025-26 (AY 2026-27)" before, where
 * the assessment year still applies. */
export function taxYearLabel(fy: number): string {
  const yy = (y: number) => String(y % 100).padStart(2, "0");
  if (fy >= 2026) return `Tax year ${fy}-${yy(fy + 1)}`;
  return `FY ${fy}-${yy(fy + 1)} (AY ${fy + 1}-${yy(fy + 2)})`;
}

/** Inclusive `[start, end]` `YYYY-MM-DD` bounds: 1 Jan - 31 Dec for a year (the
 * span the backend's dashboards `?year=` covers), first to last day for a month. One helper for every chart call site, so no chart can ask
 * for a different year span than the dashboards' `?year=`. */
export function periodRange(p: Period): { start: string; end: string } {
  if (p.mon == null) {
    return { start: `${p.year}-01-01`, end: `${p.year}-12-31` };
  }
  const mm = String(p.mon).padStart(2, "0");
  // Day 0 of the NEXT month = the last day of THIS month (UTC, so no local-tz
  // drift across the boundary).
  const lastDay = new Date(Date.UTC(p.year, p.mon, 0)).getUTCDate();
  return {
    start: `${p.year}-${mm}-01`,
    end: `${p.year}-${mm}-${String(lastDay).padStart(2, "0")}`,
  };
}

/** The `{month}` / `{year}` query params a period-taking endpoint takes —
 * supersedes the `month ? {month} : {year}` ternary duplicated at three
 * queryKey/queryFn call sites (`listSpendByCategory`, `listSpendByTag`,
 * `listTopMerchants`, `getOverview`). */
export function periodParams(p: Period): { month?: string; year?: string } {
  return p.mon != null ? { month: periodKey(p) } : { year: String(p.year) };
}
