import type {
  CategoryColor,
  CategoryKind,
  CategoryRead,
  SpendByCategoryRow,
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

export type CategoryTreeNode = CategoryRead & {
  subcategories: CategoryRead[];
};

/** Convert a flat list of categories into a two-level tree.
 * Roots (parent_id === null) own a subcategories array.
 * Orphaned subcategories (parent not found) are returned at root level so they are not lost. */
export function buildCategoryTree(
  categories: readonly CategoryRead[],
): CategoryTreeNode[] {
  const byId = new Map<number, CategoryRead>();
  const childrenByParent = new Map<number, CategoryRead[]>();
  const roots: CategoryTreeNode[] = [];

  for (const c of categories) {
    byId.set(c.id, c);
    if (c.parent_id != null) {
      const list = childrenByParent.get(c.parent_id) ?? [];
      list.push(c);
      childrenByParent.set(c.parent_id, list);
    }
  }

  for (const c of categories) {
    if (c.parent_id === null) {
      roots.push({
        ...c,
        subcategories: childrenByParent.get(c.id) ?? [],
      });
    }
  }

  // Handle any orphan subcategories whose parent_id does not exist in categories
  for (const c of categories) {
    if (c.parent_id !== null && !byId.has(c.parent_id)) {
      roots.push({
        ...c,
        subcategories: [],
      });
    }
  }

  return roots;
}

/** Lookup helper ensuring a Map<number, CategoryRead> is available */
function ensureCategoryMap(
  lookup: Map<number, CategoryRead> | readonly CategoryRead[],
): Map<number, CategoryRead> {
  if (lookup instanceof Map) return lookup;
  return new Map(lookup.map((c) => [c.id, c]));
}

/** Resolve category color with inheritance: if a subcategory has no explicit color,
 * it inherits its parent category's color. */
export function resolveCategoryColor(
  category: CategoryRead | null | undefined,
  lookup: Map<number, CategoryRead> | readonly CategoryRead[],
): CategoryColor | null {
  if (!category) return null;
  if (category.color) return category.color;
  if (category.parent_id != null) {
    const map = ensureCategoryMap(lookup);
    const parent = map.get(category.parent_id);
    if (parent?.color) return parent.color;
  }
  return null;
}

/** Format a category name as "Parent → Subcategory" or "Parent". */
export function categoryDisplayName(
  category: CategoryRead | null | undefined,
  lookup: Map<number, CategoryRead> | readonly CategoryRead[],
): string {
  if (!category) return "Uncategorized";
  if (category.parent_id != null) {
    const map = ensureCategoryMap(lookup);
    const parent = map.get(category.parent_id);
    if (parent) return `${parent.name} → ${category.name}`;
  }
  return category.name;
}

/** Get eligible parent categories for reparenting a given category.
 * Rules:
 * 1. Must be same kind (spend vs income).
 * 2. Must be a root category (parent_id === null) — max 2 levels.
 * 3. Cannot be the category itself.
 * 4. If current category already has subcategories, it cannot be assigned a parent (cannot nest). */
export function getReparentingOptions(
  currentCategory: CategoryRead | null,
  allCategories: readonly CategoryRead[],
): CategoryRead[] {
  if (!currentCategory) {
    // For creating a new category
    return allCategories.filter((c) => c.parent_id === null);
  }

  // Check if current category already has active children
  const hasChildren = allCategories.some(
    (c) => c.parent_id === currentCategory.id,
  );
  if (hasChildren) {
    return [];
  }

  return allCategories.filter(
    (c) =>
      c.parent_id === null &&
      c.kind === currentCategory.kind &&
      c.id !== currentCategory.id,
  );
}

export type SubcategorySpendItem = {
  categoryId: number | null;
  categoryName: string;
  totalPaise: number;
  isDirect?: boolean;
};

export type ParentSpendRollup = {
  parentId: number | null;
  parentName: string | null;
  totalPaise: number;
  directPaise: number;
  subcategories: SubcategorySpendItem[];
};

/**
 * Roll up a flat list of `SpendByCategoryRow` into parent categories based on
 * the 2-level taxonomy. If a row belongs to a subcategory, its total is added to
 * the parent's total and recorded in the parent's `subcategories` list.
 * Any spend assigned directly to the parent category is tracked as directPaise.
 * Uncategorized spend is preserved with `parentId: null`.
 */
export function rollUpSpendByCategory(
  rows: readonly SpendByCategoryRow[],
  allCategories: readonly CategoryRead[],
): ParentSpendRollup[] {
  const catMap = ensureCategoryMap(allCategories);
  const rollupMap = new Map<number | null, ParentSpendRollup>();

  for (const row of rows) {
    if (row.category_id == null) {
      // Uncategorized
      const existing = rollupMap.get(null) ?? {
        parentId: null,
        parentName: null,
        totalPaise: 0,
        directPaise: 0,
        subcategories: [],
      };
      existing.totalPaise += row.total_paise;
      existing.directPaise += row.total_paise;
      rollupMap.set(null, existing);
      continue;
    }

    const cat = catMap.get(row.category_id);
    if (!cat) {
      // Category not found in lookup: treat as its own parent
      const existing = rollupMap.get(row.category_id) ?? {
        parentId: row.category_id,
        parentName: row.category_name,
        totalPaise: 0,
        directPaise: 0,
        subcategories: [],
      };
      existing.totalPaise += row.total_paise;
      existing.directPaise += row.total_paise;
      rollupMap.set(row.category_id, existing);
      continue;
    }

    if (cat.parent_id != null) {
      // Subcategory: roll up to parent
      const parent = catMap.get(cat.parent_id);
      const pId = cat.parent_id;
      const pName = parent ? parent.name : (row.category_name ?? "Parent");
      const existing = rollupMap.get(pId) ?? {
        parentId: pId,
        parentName: pName,
        totalPaise: 0,
        directPaise: 0,
        subcategories: [],
      };
      existing.totalPaise += row.total_paise;
      existing.subcategories.push({
        categoryId: cat.id,
        categoryName: cat.name,
        totalPaise: row.total_paise,
      });
      rollupMap.set(pId, existing);
    } else {
      // Direct spend on parent category
      const pId = cat.id;
      const pName = cat.name;
      const existing = rollupMap.get(pId) ?? {
        parentId: pId,
        parentName: pName,
        totalPaise: 0,
        directPaise: 0,
        subcategories: [],
      };
      existing.totalPaise += row.total_paise;
      existing.directPaise += row.total_paise;
      rollupMap.set(pId, existing);
    }
  }

  // Sort subcategories inside each parent most-negative first
  for (const rollup of rollupMap.values()) {
    rollup.subcategories.sort((a, b) => a.totalPaise - b.totalPaise);
  }

  // Sort rollups: categorized first, most-negative total first, uncategorized last
  const list = Array.from(rollupMap.values());
  return list.sort((a, b) => {
    if (a.parentId === null) return 1;
    if (b.parentId === null) return -1;
    return a.totalPaise - b.totalPaise;
  });
}

/**
 * Get subcategory breakdown items for a specific parent category, including
 * a direct spend entry if there was spend directly on the parent.
 */
export function getParentSubcategorySpend(
  rows: readonly SpendByCategoryRow[],
  parentId: number,
  allCategories: readonly CategoryRead[],
): SubcategorySpendItem[] {
  const catMap = ensureCategoryMap(allCategories);
  const parent = catMap.get(parentId);
  const items: SubcategorySpendItem[] = [];
  let directPaise = 0;

  for (const row of rows) {
    if (row.category_id == null) continue;
    if (row.category_id === parentId) {
      directPaise += row.total_paise;
      continue;
    }
    const cat = catMap.get(row.category_id);
    if (cat && cat.parent_id === parentId) {
      items.push({
        categoryId: cat.id,
        categoryName: cat.name,
        totalPaise: row.total_paise,
      });
    }
  }

  // If there's direct spend on the parent, include a direct item
  if (directPaise !== 0) {
    items.push({
      categoryId: parentId,
      categoryName: `${parent?.name ?? "General"} (Direct)`,
      totalPaise: directPaise,
      isDirect: true,
    });
  }

  return items.sort((a, b) => a.totalPaise - b.totalPaise);
}


