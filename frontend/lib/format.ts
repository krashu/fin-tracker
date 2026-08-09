const inrFmt = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const dateFmt = new Intl.DateTimeFormat("en-IN", {
  month: "short",
  day: "numeric",
});

const dateYearFmt = new Intl.DateTimeFormat("en-IN", {
  year: "numeric",
  month: "short",
  day: "numeric",
});

const monthYearFmt = new Intl.DateTimeFormat("en-IN", {
  month: "long",
  year: "numeric",
});

/** Drop the sign from a negative zero before `Intl` sees it. `-0 / 100` is `-0`,
 *  and `Intl.NumberFormat` renders that as "-₹0.00" — a zero amount has no sign
 *  to a reader. `compactINR`'s docstring below carries the canonical rationale
 *  (its final branch is the same fix, inlined). Exact zero only: `|| 0` would
 *  also swallow `NaN`, changing today's "₹NaN" output. */
function unsignZero(rupees: number): number {
  return rupees === 0 ? 0 : rupees;
}

export function formatINR(paise: number): string {
  return inrFmt.format(unsignZero(paise / 100));
}

const inrWholeFmt = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 0,
});

/** Whole-rupee INR (no paise) for compact comparison baselines like "vs ₹12,049",
 * where 2dp is noise. Unlike `formatINR(...).replace(".00", "")`, this is
 * consistent across round and non-round amounts (both render 0dp). */
export function formatINRWhole(paise: number): string {
  return inrWholeFmt.format(unsignZero(paise / 100));
}

/** Compact INR for a chart Y-axis tick: ₹2.5k / ₹3.4L / ₹1.2Cr (Indian units).
 *  One decimal in the k-range (trailing ".0" trimmed) so an evenly-spaced tick
 *  like 2,500 reads "₹2.5k" — not rounded to "₹3k", which made axis labels look
 *  unevenly spaced against their gridlines. Shared by the /spending period,
 *  cashflow, and category-trend bars.
 *
 *  Sign-aware: a negative tick renders "-₹10.6k" (the cashflow net line dips
 *  below zero on a deficit month). Non-negative input is byte-identical to the
 *  pre-sign version, so the spend-bar caller (always ≥ 0) is unaffected. The
 *  final branch drops the sign when the rounded magnitude is 0, so a sub-₹0.5
 *  negative tick reads "₹0", never "-₹0". */
export function compactINR(paise: number): string {
  const sign = paise < 0 ? "-" : "";
  const r = Math.abs(paise) / 100;
  if (r >= 1e7) return `${sign}₹${(r / 1e7).toFixed(1)}Cr`;
  if (r >= 1e5) return `${sign}₹${(r / 1e5).toFixed(1)}L`;
  if (r >= 1e3) return `${sign}₹${(r / 1e3).toFixed(1).replace(/\.0$/, "")}k`;
  const rounded = Math.round(r);
  return `${rounded === 0 ? "" : sign}₹${rounded}`;
}

/** Parse a user-typed rupee string ("1234.50") to integer paise. Returns 0 for
 *  blank/NaN input — most callers guard on magnitude > 0 before submitting, but
 *  the `|| 0` fallback also lets a blank field mean a zero opening balance (the
 *  accounts form accepts 0). The trailing round recovers the nearest paise from
 *  2-decimal input. Returns an unsigned magnitude; sign (spend / credit-card
 *  debt) stays at the call-site. */
export function rupeesToPaise(input: string): number {
  return Math.round((parseFloat(input) || 0) * 100);
}

/** The inverse: integer paise → the plain rupee string a number input expects.
 *  Deliberately NOT `formatINR` — that emits grouping separators and a ₹ sign,
 *  which `<input type="number">` rejects, and round-tripping it back through
 *  `rupeesToPaise` would parse "1,234.50" as 1. Two decimals always, so an edit
 *  that doesn't touch the field re-submits the identical paise value and the
 *  minimal-PATCH diff stays a no-op. Pass a magnitude; sign lives at the
 *  call-site, same as `rupeesToPaise`. */
export function paiseToRupees(paise: number): string {
  return (paise / 100).toFixed(2);
}

const moneyFmts = new Map<string, Intl.NumberFormat>();

function moneyFmt(currency: "INR" | "USD"): Intl.NumberFormat {
  let fmt = moneyFmts.get(currency);
  if (!fmt) {
    fmt = new Intl.NumberFormat("en-IN", {
      style: "currency",
      currency,
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
    moneyFmts.set(currency, fmt);
  }
  return fmt;
}

/** Currency-aware money formatter (₹ for INR, $ for USD). Use when the currency
 * may not be INR — e.g. an investment account in USD; `formatINR` always emits ₹. */
export function formatMoney(paise: number, currency: "INR" | "USD"): string {
  return moneyFmt(currency).format(unsignZero(paise / 100));
}

/** Money that arrives as a native-currency **decimal string** (avg cost, per-unit
 * price, NAV) rather than integer paise. Parses for display only — the exact value
 * lives server-side; display rounding to 2dp is fine. Falls back to the raw string
 * if unparseable. */
export function formatDecimalMoney(
  value: string,
  currency: "INR" | "USD",
): string {
  const n = parseFloat(value);
  return Number.isFinite(n) ? moneyFmt(currency).format(n) : value;
}

/** Format a holding/transaction unit quantity (a decimal string, up to 8dp) for
 * display, trimming trailing zeros. Display-only parse; falls back to the raw
 * string if unparseable. */
export function formatUnits(value: string, maxDp = 4): string {
  const n = parseFloat(value);
  return Number.isFinite(n)
    ? n.toLocaleString("en-IN", { maximumFractionDigits: maxDp })
    : value;
}

/** A date for list/table display. Year-aware: omitted for the current year
 * ("15 Jun" — compact, the common case) and shown for any other year
 * ("15 Jun 2024") so multi-year data is never ambiguous. The "current year" is
 * read at call time; every caller is a client island, so there's no SSR
 * hydration mismatch. For a date RANGE use `formatDateRange` (it decides the
 * year once for both endpoints, so the label can't go asymmetric at a boundary). */
export function formatDate(iso: string): string {
  const d = new Date(iso);
  const fmt =
    d.getFullYear() === new Date().getFullYear() ? dateFmt : dateYearFmt;
  return fmt.format(d);
}

/** A date that ALWAYS shows the year ("15 Jun 2026") — unlike `formatDate`, which
 * drops it for the current year. Use where the year must never be inferred, e.g.
 * the import review queue: a freshly parsed statement can be from any year, and
 * the user is confirming exactly what will be committed, so the year is load-
 * bearing even when it happens to be the current one. */
export function formatDateWithYear(iso: string): string {
  return dateYearFmt.format(new Date(iso));
}

/** A start–end range for display ("1 Jun – 30 Jun", or "1 Dec 2025 – 31 Dec 2025").
 * The year is decided ONCE for the whole range — shown on both endpoints when
 * either falls outside the current year (or the range crosses a year), never on
 * just one — so a year-boundary comparison reads symmetrically. */
export function formatDateRange(startIso: string, endIso: string): string {
  const start = new Date(startIso);
  const end = new Date(endIso);
  const currentYear = new Date().getFullYear();
  const fmt =
    start.getFullYear() === currentYear && end.getFullYear() === currentYear
      ? dateFmt
      : dateYearFmt;
  return `${fmt.format(start)} – ${fmt.format(end)}`;
}

/** "June 2026" — the SummaryStrip month anchor label. */
export function formatMonthYear(d: Date): string {
  return monthYearFmt.format(d);
}

const monthOnlyFmt = new Intl.DateTimeFormat("en-IN", { month: "long" });

/** "June" — month name only, used by the filter row when the year is shown
 * separately in the year dropdown. */
export function formatMonth(d: Date): string {
  return monthOnlyFmt.format(d);
}

const pctFmt = new Intl.NumberFormat("en-IN", {
  style: "percent",
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

/** Format a fraction as a percentage ("0.623" → "62.3%"). Sign-agnostic: the
 * caller prepends its own ± and color, so pass `Math.abs(fraction)` for a signed
 * display. Used for allocation % and XIRR, which are ratios — never money, never
 * masked by hide-balance. */
export function formatPercent(fraction: number): string {
  return pctFmt.format(fraction);
}
