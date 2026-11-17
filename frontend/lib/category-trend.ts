/**
 * Pure series-building for the category trend card (PRD §F8 view 6). The
 * endpoint returns every parent with an in-window row; folding to the top few,
 * colouring and signing for the chart all live here so they are unit-testable
 * without a DOM. Values stay signed (spend negative) until `chartRows`.
 */
import type { CategoryTrendParent, CategoryTrendResponse } from "@/lib/api/client";
import { categoryColorVar } from "@/lib/categories";

/** Series shown before the rest fold into one hatched series. A fold holding a
 * single series would be pointless, so folding starts above `TREND_TOP_N + 1`. */
export const TREND_TOP_N = 6;

export type TrendSeries = {
  /** "c<id>" | "fold" | "uncat" | "direct" */
  key: string;
  /** Includes " (archived)" where archived. */
  label: string;
  /** CSS colour (fold: the hatch stroke colour). */
  color: string;
  /** The fold is hatched, never a solid gray (it can't be told from "Other"). */
  pattern: "solid" | "hatch";
  kind: "category" | "fold" | "uncategorized" | "direct";
  /** Drill target (overview, kind "category" only). */
  categoryId: number | null;
  drillable: boolean;
  /** Signed, aligned to the response's periods. */
  totalsPaise: number[];
};

const sum = (xs: readonly number[]): number => xs.reduce((a, b) => a + b, 0);

const archivedLabel = (name: string, archived: boolean): string =>
  archived ? `${name} (archived)` : name;

function addInto(acc: number[], xs: readonly number[]): void {
  xs.forEach((v, i) => {
    acc[i] += v;
  });
}

/** Most spend first (totals are negative, so ascending), ties to the lower id. */
function rankBySpend<T>(items: readonly T[], total: (t: T) => number, id: (t: T) => number): T[] {
  return [...items].sort((a, b) => total(a) - total(b) || id(a) - id(b));
}

function foldSeries(rest: readonly number[][], noun: string): TrendSeries {
  const totals = rest[0].map(() => 0);
  for (const t of rest) addInto(totals, t);
  return {
    key: "fold",
    label: `${rest.length} more ${noun}`,
    color: "var(--muted-foreground)",
    pattern: "hatch",
    kind: "fold",
    categoryId: null,
    drillable: false,
    totalsPaise: totals,
  };
}

/** `fold: false` lists every series unfolded (the table view, which is also the
 * keyboard path to a folded parent). */
export type SeriesOptions = { fold?: boolean };

export function overviewSeries(
  resp: CategoryTrendResponse,
  { fold = true }: SeriesOptions = {},
): TrendSeries[] {
  const categorized = rankBySpend(
    resp.parents.filter((p) => p.parent_id != null),
    (p) => sum(p.totals_paise),
    (p) => p.parent_id ?? 0,
  );
  const shown =
    !fold || categorized.length <= TREND_TOP_N + 1
      ? categorized
      : categorized.slice(0, TREND_TOP_N);
  const folded = categorized.slice(shown.length);

  const series: TrendSeries[] = shown.map((p) => ({
    key: `c${p.parent_id}`,
    label: archivedLabel(p.parent_name ?? "Category", p.archived),
    // The entity's own hue, so it is stable across ranks and tag filters.
    color: categoryColorVar(p.parent_id, p.color),
    pattern: "solid",
    kind: "category",
    categoryId: p.parent_id,
    drillable: true,
    totalsPaise: p.totals_paise,
  }));
  if (folded.length > 0) {
    series.push(foldSeries(folded.map((p) => p.totals_paise), "categories"));
  }
  const uncat = resp.parents.find((p) => p.parent_id == null);
  if (uncat) {
    series.push({
      key: "uncat",
      label: "Uncategorized",
      color: "var(--chart-uncategorized)",
      pattern: "solid",
      kind: "uncategorized",
      categoryId: null,
      drillable: false,
      totalsPaise: uncat.totals_paise,
    });
  }
  return series;
}

export function drillSeries(
  parent: CategoryTrendParent,
  { fold = true }: SeriesOptions = {},
): TrendSeries[] {
  const subs = rankBySpend(
    parent.subcategories.filter((s) => !s.is_direct),
    (s) => sum(s.totals_paise),
    (s) => s.category_id,
  );
  const shown =
    !fold || subs.length <= TREND_TOP_N + 1 ? subs : subs.slice(0, TREND_TOP_N);
  const folded = subs.slice(shown.length);

  const series: TrendSeries[] = shown.map((s) => ({
    key: `c${s.category_id}`,
    label: archivedLabel(s.category_name ?? "Subcategory", s.archived),
    color: categoryColorVar(s.category_id, s.color),
    pattern: "solid",
    kind: "category",
    categoryId: s.category_id,
    drillable: false,
    totalsPaise: s.totals_paise,
  }));
  if (folded.length > 0) {
    series.push(foldSeries(folded.map((s) => s.totals_paise), "subcategories"));
  }
  const direct = parent.subcategories.find((s) => s.is_direct);
  if (direct) {
    series.push({
      key: "direct",
      label: `${archivedLabel(direct.category_name ?? parent.parent_name ?? "Category", direct.archived)} (direct)`,
      // The parent's own hue is reserved for this row.
      color: categoryColorVar(parent.parent_id, parent.color),
      pattern: "solid",
      kind: "direct",
      categoryId: direct.category_id,
      drillable: false,
      totalsPaise: direct.totals_paise,
    });
  }
  return series;
}

/** Recharts rows: spend plots positive, a net-credit month goes negative. */
export function chartRows(
  periods: readonly string[],
  series: readonly TrendSeries[],
): Record<string, string | number>[] {
  return periods.map((period, i) => {
    const row: Record<string, string | number> = { period };
    for (const s of series) row[s.key] = 0 - s.totalsPaise[i];
    return row;
  });
}
