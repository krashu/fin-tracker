/** Local YYYY-MM-DD — never toISOString() (a UTC shift moves the day in IST). */
export function toLocalYMD(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
    d.getDate(),
  ).padStart(2, "0")}`;
}

/** First-of-month Date for the current local month. Shared by the /spending
 * spend-by-category stepper and the /expenses month filter so both anchor to
 * the same "current month" (and the same query cache shape). */
export function thisMonthAnchor(): Date {
  const n = new Date();
  return new Date(n.getFullYear(), n.getMonth(), 1);
}

/** A first-of-month Date → "YYYY-MM" (local, never toISOString()). Used as a
 * stable string query-key fragment — never put a raw Date in a query key. */
export function monthKey(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

/** `{date_from, date_to}` spanning the full calendar month of `anchor` (first →
 * last day) as local YYYY-MM-DD. `day 0` of the next month is the last day of
 * this one; `toLocalYMD` keeps it off the toISOString day-shift trap. */
export function monthRange(anchor: Date): { date_from: string; date_to: string } {
  const first = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
  const last = new Date(anchor.getFullYear(), anchor.getMonth() + 1, 0);
  return { date_from: toLocalYMD(first), date_to: toLocalYMD(last) };
}

/** `{start, end}` for [Monday `weeksBack` weeks ago .. today] — Monday-aligned so
 * the leftmost weekly bucket isn't a clipped partial week. With weeksBack=13 the
 * backend returns 14 ISO-week buckets (13 full + the current partial week).
 *
 * Shared by the SummaryStrip sparkline and the /spending period bar so their
 * `spend-by-period` queries hit the SAME TanStack cache key — keep it here, not
 * copied, or the two silently desync (double-fetch). Local Y/M/D only. */
export function trailingWeeksWindow(
  now: Date,
  weeksBack: number,
): { start: string; end: string } {
  const toMonday = (now.getDay() + 6) % 7; // getDay(): Sun=0 → Mon-offset
  const start = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate() - toMonday - weeksBack * 7,
  );
  return { start: toLocalYMD(start), end: toLocalYMD(now) };
}

/** `{start, end}` for [first-of-month `monthsBack` months ago .. today] as local
 * YYYY-MM-DD. `monthsBack=11` yields 12 month buckets (11 prior + the current
 * partial month), mirroring `trailingWeeksWindow`'s "13 weeks → 14 buckets".
 * Shared by the /spending monthly period bar, cashflow bar, and category-trend
 * bar so their `spend-by-period` / `cashflow-by-period` / `spend-by-category-
 * by-period` month queries resolve to one cache-key shape. Local Y/M/D only —
 * never toISOString(). */
export function trailingMonths(
  now: Date,
  monthsBack: number,
): { start: string; end: string } {
  const start = new Date(now.getFullYear(), now.getMonth() - monthsBack, 1);
  return { start: toLocalYMD(start), end: toLocalYMD(now) };
}
