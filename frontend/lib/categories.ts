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

/** CSS colour for a server-resolved category colour (`display_color`, or a trend series
 * `color`); `null`/absent (uncategorised) renders `--muted-foreground`. The `id` arg is
 * unused. */
export function categoryColorVar(
  _id: number | null,
  color?: CategoryColor | null,
): string {
  return color ?? "var(--muted-foreground)";
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

/** What an id with no name on the wire (and no active row) is called. */
const ARCHIVED_CATEGORY_LABEL = "Archived category";

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
 * breadcrumb), `SpendByCategoryRow` (`category_name` +
 * `parent_name`, so an archived subcategory keeps its breadcrumb) and
 * `CategoryTrendSubcategory.category_name` (own name only — that aggregate carries no
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
  if (!storedName) return ARCHIVED_CATEGORY_LABEL;
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
 * the 2-level taxonomy. A row with a `parent_id` is a subcategory: its total is
 * added to the parent's total and recorded in the parent's `subcategories` list.
 * Any spend assigned directly to the parent category is tracked as directPaise.
 * Uncategorized spend is preserved with `parentId: null`.
 *
 * Works from the rows alone (ADR-0012 rule 6): each row carries its own
 * `parent_id` / `parent_name` whatever the archive state, so an archived
 * subcategory still rolls up under its parent, which the active-only category
 * list could not place.
 */
export function rollUpSpendByCategory(
  rows: readonly SpendByCategoryRow[],
): ParentSpendRollup[] {
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

    if (row.parent_id != null) {
      // Subcategory: roll up to parent
      const pId = row.parent_id;
      const existing = rollupMap.get(pId) ?? {
        parentId: pId,
        parentName: row.parent_name ?? ARCHIVED_CATEGORY_LABEL,
        totalPaise: 0,
        directPaise: 0,
        subcategories: [],
      };
      existing.totalPaise += row.total_paise;
      existing.subcategories.push({
        categoryId: row.category_id,
        categoryName: row.category_name ?? ARCHIVED_CATEGORY_LABEL,
        totalPaise: row.total_paise,
      });
      rollupMap.set(pId, existing);
    } else {
      // Direct spend on a root category
      const pId = row.category_id;
      const existing = rollupMap.get(pId) ?? {
        parentId: pId,
        parentName: row.category_name ?? ARCHIVED_CATEGORY_LABEL,
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
 * a direct spend entry if there was spend directly on the parent. Like
 * `rollUpSpendByCategory`, it matches on the row's own `parent_id`, so an
 * archived subcategory is listed too.
 */
export function getParentSubcategorySpend(
  rows: readonly SpendByCategoryRow[],
  parentId: number,
): SubcategorySpendItem[] {
  const items: SubcategorySpendItem[] = [];
  let directPaise = 0;
  let parentName: string | null = null;

  for (const row of rows) {
    if (row.category_id == null) continue;
    if (row.category_id === parentId) {
      directPaise += row.total_paise;
      parentName = row.category_name;
      continue;
    }
    if (row.parent_id === parentId) {
      parentName ??= row.parent_name;
      items.push({
        categoryId: row.category_id,
        categoryName: row.category_name ?? ARCHIVED_CATEGORY_LABEL,
        totalPaise: row.total_paise,
      });
    }
  }

  // If there's direct spend on the parent, include a direct item
  if (directPaise !== 0) {
    items.push({
      categoryId: parentId,
      categoryName: `${parentName ?? "General"} (Direct)`,
      totalPaise: directPaise,
      isDirect: true,
    });
  }

  return items.sort((a, b) => a.totalPaise - b.totalPaise);
}
