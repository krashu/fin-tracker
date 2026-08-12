import type {
  CategoryColor,
  CategoryKind,
  TransactionType,
} from "@/lib/api/client";

/** Which category scope a transaction type draws from: income transactions use
 * income categories; spend uses spend categories, of either sign — a refund is
 * a positive-amount spend (ADR-0009), so it nets against spend in the same
 * category rather than drawing from a scope of its own. Transfers take the
 * "spend" fallback and that path IS reached through the UI — a transfer may
 * carry a spend category (ADR-0007 rule 7), and both the row dialog and the
 * board's Transfers view offer the spend picker on one. Single source of
 * truth for the four category pickers. */
export function categoryKindForType(t: TransactionType): CategoryKind {
  return t === "income" ? "income" : "spend";
}

/** Human label for a category kind (settings list headers, etc.). */
export const CATEGORY_KIND_LABELS: Record<CategoryKind, string> = {
  spend: "Spend",
  income: "Income",
};

/** CSS color value for a category's dot/bar/swatch. Color is purely the user's
 * choice (a `#rrggbb` hex picked in settings) — there is no auto-generated or
 * derived default. A category with no chosen color (and the uncategorized
 * bucket) renders a neutral muted dot. The `id` arg is unused (kept for a
 * stable call signature across the dots/bars). */
export function categoryColorVar(
  _id: number | null,
  color?: CategoryColor | null,
): string {
  return color ?? "var(--muted-foreground)";
}

/** Curated categorical swatch palette for category colors. The picker offers
 * exactly these; a freeform picker is overkill for tagging and risks clashing /
 * low-contrast colors.
 *
 * The order is load-bearing, not cosmetic: `nextCategoryColor` hands out slots
 * in sequence, and a chart colours its series in the same order, so array
 * neighbours are visual neighbours. This order is validated (OKLab CVD/contrast,
 * both light `#ffffff` and dark `#171c22` surfaces) — every adjacent pair clears
 * the ≥8 CVD target and the ≥15 normal-vision floor in both modes. The first
 * eight hues are additionally a fully CVD-safe categorical set on their own (the
 * method's guaranteed ceiling); the remaining six extend the picker for finance's
 * many categories and stay adjacent-safe, but past eight *simultaneously* shown
 * series lean on the direct labels the spend-by-category view already draws.
 * A single hex per slot renders in both themes (categories store one colour);
 * these were tuned to clear both surfaces, so don't "brighten" them per-theme. */
export const CATEGORY_PALETTE: readonly CategoryColor[] = [
  "#2a78d6", // blue
  "#008300", // green
  "#d55181", // magenta
  "#c98500", // amber
  "#1baf7a", // aqua
  "#d95926", // orange
  "#6c5cd6", // violet
  "#e34948", // red
  "#0e97c4", // cyan
  "#c23b6b", // raspberry
  "#6f9e15", // lime
  "#b246c0", // purple
  "#a9682f", // brown
  "#0e9488", // teal
];

/** The first palette color not already used by an existing category, so a new
 * category gets a distinct hue automatically (no picking required). Wraps once
 * there are more categories than palette entries. */
export function nextCategoryColor(
  used: readonly (CategoryColor | null)[],
): CategoryColor {
  const taken = new Set(used.filter((c): c is CategoryColor => c != null));
  return (
    CATEGORY_PALETTE.find((c) => !taken.has(c)) ??
    CATEGORY_PALETTE[taken.size % CATEGORY_PALETTE.length]
  );
}
