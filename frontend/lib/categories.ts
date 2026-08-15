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

  // Promote any subcategory whose parent is absent from this (possibly
  // pre-filtered — category-selector.tsx, categories-manager.tsx,
  // review-queue.tsx all call this on a filtered list) input, OR present but
  // itself not a root. The latter is a defence-in-depth case: the backend caps
  // depth at 2, so a "grandchild" can't exist in honest full data, but a
  // filtered list can make a genuine child look like a third level if its
  // actual parent got filtered out while something else took that id's slot
  // in a stale render. Either way the node must not silently disappear
  // (ADR-0012: both tree builders must agree a dropped row is never correct).
  for (const c of categories) {
    if (c.parent_id !== null) {
      const parent = byId.get(c.parent_id);
      if (!parent || parent.parent_id !== null) {
        roots.push({
          ...c,
          subcategories: [],
        });
      }
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

function hexToRgb(hex: string): [number, number, number] {
  const n = parseInt(hex.replace("#", ""), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function rgbToHex(r: number, g: number, b: number): CategoryColor {
  const clamp = (v: number) => Math.max(0, Math.min(255, Math.round(v)));
  return `#${[r, g, b].map((v) => clamp(v).toString(16).padStart(2, "0")).join("")}`;
}

function rgbToHsl(r: number, g: number, b: number): [number, number, number] {
  const rn = r / 255;
  const gn = g / 255;
  const bn = b / 255;
  const max = Math.max(rn, gn, bn);
  const min = Math.min(rn, gn, bn);
  const l = (max + min) / 2;
  const d = max - min;
  if (d === 0) return [0, 0, l];
  const s = d / (1 - Math.abs(2 * l - 1));
  let h: number;
  if (max === rn) h = ((gn - bn) / d) % 6;
  else if (max === gn) h = (bn - rn) / d + 2;
  else h = (rn - gn) / d + 4;
  h *= 60;
  if (h < 0) h += 360;
  return [h, s, l];
}

function hslToRgb(h: number, s: number, l: number): [number, number, number] {
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = l - c / 2;
  let r = 0;
  let g = 0;
  let b = 0;
  if (h < 60) [r, g, b] = [c, x, 0];
  else if (h < 120) [r, g, b] = [x, c, 0];
  else if (h < 180) [r, g, b] = [0, c, x];
  else if (h < 240) [r, g, b] = [0, x, c];
  else if (h < 300) [r, g, b] = [x, 0, c];
  else [r, g, b] = [c, 0, x];
  return [(r + m) * 255, (g + m) * 255, (b + m) * 255];
}

/** Fixed HSL-lightness deltas, cycled by sibling index. Symmetric around 0 so
 * early siblings don't all skew lighter (or darker) in the same direction;
 * six entries cover the seeded taxonomy's largest sibling groups without a
 * repeat, and the cycle repeats past that rather than growing unboundedly. */
const SHADE_LIGHTNESS_DELTA = [0, -0.16, 0.14, -0.28, 0.24, -0.08] as const;

/**
 * A distinct shade of `parentHue` for the sibling at `siblingIndex` — locked
 * decision #5 (color inherits one hop; siblings are separated by a derived
 * shade of the parent hue, never an unrelated colour). Takes the *resolved*
 * hue (`resolveCategoryColor`'s output, never a raw `category.color`, which
 * can't inherit and renders grey for a `NULL` child — see `CategoryDot`) and
 * varies HSL lightness by a fixed per-index delta, clamped well clear of
 * white/black so the result still reads as the same family. Purely a display
 * derivation: never stored, never round-tripped through the API.
 *
 * `resolveCategoryColor` itself is untouched by this — it keeps returning the
 * plain family hue for a lone dot (e.g. the board row, one transaction at a
 * time). Apply this shade only where two siblings render side by side: a flat
 * list, a drilldown, a dropdown.
 */
export function siblingShade(
  parentHue: CategoryColor,
  siblingIndex: number,
): CategoryColor {
  const [r, g, b] = hexToRgb(parentHue);
  const [h, s, l] = rgbToHsl(r, g, b);
  const delta =
    SHADE_LIGHTNESS_DELTA[
      ((siblingIndex % SHADE_LIGHTNESS_DELTA.length) +
        SHADE_LIGHTNESS_DELTA.length) %
        SHADE_LIGHTNESS_DELTA.length
    ];
  const shadedL = Math.min(0.82, Math.max(0.18, l + delta));
  const [nr, ng, nb] = hslToRgb(h, s, shadedL);
  return rgbToHex(nr, ng, nb);
}

/**
 * Safely derives a shade for a subcategory within a hierarchical chart.
 * If the parent has no hex color (e.g. Uncategorized or CSS variable),
 * it safely falls back to a neutral CSS variable.
 */
export function deriveSubcategoryColor(
  parentHue: CategoryColor | string | null | undefined,
  siblingIndex: number,
  _totalSiblings?: number,
): string {
  if (!parentHue || !parentHue.startsWith("#")) {
    return "var(--muted-foreground)";
  }
  return siblingShade(parentHue as CategoryColor, siblingIndex);
}


/**
 * Resolve the *display* color for a category shown alongside its siblings —
 * `resolveCategoryColor`'s hue, shaded by `siblingShade` when the category is
 * an inheriting child (no color of its own, per locked decision #5's `NULL`
 * seed). A root, or a child carrying its own explicit color, renders as-is —
 * only the inherited case needs separating from its siblings. The sibling
 * index is derived from `allCategories` sorted by id, not from the order rows
 * happen to appear in a particular query result, so a category's shade stays
 * the same across periods and views.
 */
export function resolveSiblingDisplayColor(
  category: CategoryRead | null | undefined,
  allCategories: readonly CategoryRead[],
): CategoryColor | null {
  if (!category) return null;
  const resolved = resolveCategoryColor(category, ensureCategoryMap(allCategories));
  if (!resolved) return null;
  if (category.color || category.parent_id == null) return resolved;
  const siblings = allCategories
    .filter((c) => c.parent_id === category.parent_id)
    .sort((a, b) => a.id - b.id);
  const index = siblings.findIndex((c) => c.id === category.id);
  return siblingShade(resolved, index < 0 ? 0 : index);
}

/** Display label for a category **id**, tolerating an ARCHIVED category.
 *
 * `GET /categories` returns active rows only (`categories.py`, no
 * `include_archived` exists), so a historical row pointing at an archived
 * category misses `lookup`. Passing that miss to `categoryDisplayName` answers
 * "Uncategorized" — a *different fact*, and a lie about the user's data: the
 * archive dialog promises "existing transactions will keep their historical
 * categories", and the FK genuinely survives (`test_soft_delete_keeps_transactions`).
 *
 * So resolve by id, not by an already-missed object:
 * - no id at all            → genuinely "Uncategorized"
 * - id present, row active   → the full "Parent → Child" breadcrumb
 * - id present, row archived → `storedName` if the caller has it on the wire,
 *   else "Archived category"
 *
 * Callers with a denormalized name on the wire: `TransactionRead`
 * (`category_name` + `category_parent_name`, so an archived row keeps its
 * breadcrumb), `SpendByCategoryRow.category_name` and
 * `SpendCategoryRef.category_name` (own name only — those aggregates carry no
 * parent, so an archived row there renders unqualified). All are joined on
 * id + `user_id`, deliberately never `archived_at`.
 *
 * Mirrors the accounts precedent — `accounts-manager.tsx`'s
 * `Archived account (#id)` — whose comment states the same principle: absent
 * from the active list is not the same as unset.
 */
export function categoryLabel(
  categoryId: number | null | undefined,
  lookup: Map<number, CategoryRead> | readonly CategoryRead[],
  storedName?: string | null,
  storedParentName?: string | null,
): string {
  if (categoryId == null) return "Uncategorized";
  const map = ensureCategoryMap(lookup);
  const category = map.get(categoryId);
  // Active rows resolve through the live list, never the stored copy, so a
  // rename shows up without refetching every transaction that references it.
  if (category) return categoryDisplayName(category, map);
  if (!storedName) return "Archived category";
  return storedParentName ? `${storedParentName} → ${storedName}` : storedName;
}

/** True when an id points at a category absent from the active list — i.e.
 * archived. Separate from {@link categoryLabel} so a caller can decorate the
 * label (an "(archived)" marker) without re-resolving it. */
export function isArchivedCategoryId(
  categoryId: number | null | undefined,
  lookup: Map<number, CategoryRead> | readonly CategoryRead[],
): boolean {
  if (categoryId == null) return false;
  return !ensureCategoryMap(lookup).has(categoryId);
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

/** Get eligible parent categories for reparenting a given category (or, when
 * `currentCategory` is `null`, for a category being newly created). Rules:
 * 1. Must match `kind` (spend vs income) — the caller's requested kind, not
 *    necessarily `currentCategory.kind`: create-mode has no category yet, and
 *    edit-mode's kind is immutable but still driven by the same form state.
 * 2. Must be an active (non-archived) root category (`parent_id === null`) —
 *    max 2 levels. `allCategories` today only ever holds active rows (`GET
 *    /categories` filters `archived_at IS NULL` server-side — see
 *    `test_list_omits_archived`), so this check is currently a no-op; it's
 *    kept because this is an exported, general-purpose helper and its own
 *    contract shouldn't quietly depend on every future caller sourcing data
 *    the same way (8.4).
 * 3. Cannot be the category itself.
 * 4. If current category already has subcategories, it cannot be assigned a
 *    parent (cannot nest).
 * Both the kind and archived filters used to be left to the caller
 * (`categories-manager.tsx` re-filtered by kind after calling this); pushed
 * in here so there's one place that knows the eligibility rule. */
export function getReparentingOptions(
  currentCategory: CategoryRead | null,
  allCategories: readonly CategoryRead[],
  kind: CategoryKind,
): CategoryRead[] {
  // Check if current category already has active children
  const hasChildren =
    currentCategory != null &&
    allCategories.some((c) => c.parent_id === currentCategory.id);
  if (hasChildren) {
    return [];
  }

  return allCategories.filter(
    (c) =>
      c.parent_id === null &&
      c.kind === kind &&
      c.archived_at == null &&
      c.id !== currentCategory?.id,
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

  // A parent's own direct spend needs a row in `subcategories` too — not just
  // in `directPaise` — so the count that gates the drilldown button and the
  // "{n} subcat(s)" badge (spend-by-category.tsx) matches what the drilldown
  // itself renders (`getParentSubcategorySpend` already synthesizes this same
  // "(Direct)" row). Uncategorized (`parentId: null`) has no such concept.
  for (const rollup of rollupMap.values()) {
    if (rollup.parentId != null && rollup.directPaise !== 0) {
      rollup.subcategories.push({
        categoryId: rollup.parentId,
        categoryName: `${rollup.parentName ?? "General"} (Direct)`,
        totalPaise: rollup.directPaise,
        isDirect: true,
      });
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
