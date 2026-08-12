/**
 * A period is either a calendar month or a calendar year — the same
 * month-or-year period the backend's `PeriodWindow` accepts (`app/api/v1/
 * dashboards.py`). One value type + four helpers, replacing what used to be
 * `selectedYear` + `selectedMonth` hand-synced separately in three
 * components (overview.tsx, spending-dashboard.tsx, expenses-board.tsx),
 * each re-deriving its own copy of the query-key/query-fn param ternary, the
 * display label, and the `[start, end]` range.
 *
 * Sits beside `lib/dates.ts` (window math) and `lib/categories.ts` (category
 * value semantics) — the same small-value-module idiom (frontend/CLAUDE.md
 * §Charts already pins period/window math to `lib/dates.ts`).
 */

/** `mon` absent = a whole-year period; present (1-12) = that calendar month. */
export type Period = { year: number; mon?: number };

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

/** Inclusive `[start, end]` `YYYY-MM-DD` calendar bounds. Supersedes the
 * `${year}-01-01` / `${year}-12-31` pair hand-built at each of five chart
 * call sites for the year case, and the month-first/month-last pair for the
 * month case. */
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
