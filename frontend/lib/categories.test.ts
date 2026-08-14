import { describe, expect, it } from "vitest";
import type { CategoryRead, SpendByCategoryRow } from "@/lib/api/client";
import {
  buildCategoryTree,
  categoryLabel,
  getParentSubcategorySpend,
} from "@/lib/categories";

function cat(overrides: Partial<CategoryRead> & Pick<CategoryRead, "id" | "name">): CategoryRead {
  return {
    kind: "spend",
    is_seeded: false,
    archived_at: null,
    color: null,
    parent_id: null,
    ...overrides,
  };
}

describe("buildCategoryTree", () => {
  it("nests children under their parent as roots with a subcategories array", () => {
    const groceries = cat({ id: 1, name: "Groceries" });
    const storeA = cat({ id: 2, name: "Store A", parent_id: 1 });
    const transport = cat({ id: 3, name: "Transport" });
    const fuel = cat({ id: 4, name: "Fuel", parent_id: 3 });

    const roots = buildCategoryTree([groceries, storeA, transport, fuel]);

    expect(roots).toHaveLength(2);
    const groceriesRoot = roots.find((r) => r.id === 1);
    const transportRoot = roots.find((r) => r.id === 3);
    expect(groceriesRoot?.subcategories.map((c) => c.id)).toEqual([2]);
    expect(transportRoot?.subcategories.map((c) => c.id)).toEqual([4]);
  });

  // ADR-0012: archiving cascades one level and stays soft, and a filtered list
  // (by kind, or because the parent got archived out of the active set) can
  // leave a child's parent absent from the result set. buildCategoryTree must
  // promote that child to a root rather than silently dropping it — this is
  // the exact defect class the arc shipped fixes for (a dropped row is never
  // correct, per the doc comment on buildCategoryTree itself).
  it("promotes an orphaned subcategory to a root instead of dropping it", () => {
    // Parent id 99 is absent entirely — simulates an archived-and-filtered-out
    // parent, or a parent excluded by a `kind` filter upstream.
    const orphan = cat({ id: 5, name: "Streaming", parent_id: 99 });

    const roots = buildCategoryTree([orphan]);

    expect(roots).toHaveLength(1);
    expect(roots[0].id).toBe(5);
    expect(roots[0].subcategories).toEqual([]);
  });
});

describe("getParentSubcategorySpend", () => {
  // Deliberate behaviour (see the doc comment above rollUpSpendByCategory's
  // "(Direct)" synthesis in lib/categories.ts, and the "Do not fix these" note
  // in plans/category-hierarchy-remaining.md): direct spend on the parent
  // itself is surfaced as a synthetic "(Direct)" row alongside the real child
  // breakdown, so the count driving the "{n} rows" badge in spend-by-category.tsx
  // matches what the drilldown list actually renders. Do NOT "fix" this to
  // exclude the direct row from the count — only its label was ever a bug.
  it("includes a synthetic (Direct) row so the count matches the drilldown", () => {
    const parent = cat({ id: 1, name: "Groceries" });
    const child = cat({ id: 2, name: "Store A", parent_id: 1 });
    const categories = [parent, child];

    const rows: SpendByCategoryRow[] = [
      { category_id: 2, category_name: "Store A", total_paise: -50000 },
      { category_id: 1, category_name: "Groceries", total_paise: -12000 },
    ];

    const items = getParentSubcategorySpend(rows, 1, categories);

    // One real child + one synthetic direct row = 2, not 1.
    expect(items).toHaveLength(2);
    const direct = items.find((i) => i.isDirect);
    expect(direct).toBeDefined();
    expect(direct?.categoryName).toBe("Groceries (Direct)");
    expect(direct?.categoryId).toBe(1);
    expect(direct?.totalPaise).toBe(-12000);
  });

  it("omits the (Direct) row when there is no spend directly on the parent", () => {
    const parent = cat({ id: 1, name: "Groceries" });
    const child = cat({ id: 2, name: "Store A", parent_id: 1 });
    const categories = [parent, child];

    const rows: SpendByCategoryRow[] = [
      { category_id: 2, category_name: "Store A", total_paise: -50000 },
    ];

    const items = getParentSubcategorySpend(rows, 1, categories);

    expect(items).toHaveLength(1);
    expect(items.every((i) => !i.isDirect)).toBe(true);
  });
});

describe("categoryLabel", () => {
  it("resolves an active category's name through the live list, not the stored wire copy", () => {
    // The lookup has the CURRENT (renamed) name; storedName/storedParentName
    // simulate a stale copy carried on an old transaction row. An active
    // category must win via the live list so a rename shows up without
    // refetching every transaction that references it.
    const parent = cat({ id: 1, name: "Groceries (renamed)" });
    const child = cat({ id: 2, name: "Store A (renamed)", parent_id: 1 });
    const lookup = new Map([
      [1, parent],
      [2, child],
    ]);

    const label = categoryLabel(2, lookup, "Store A (stale)", "Groceries (stale)");

    expect(label).toBe("Groceries (renamed) → Store A (renamed)");
  });

  it("falls back to the stored wire copy when the category is archived (absent from the live list)", () => {
    const lookup = new Map<number, CategoryRead>(); // empty: nothing active

    const label = categoryLabel(2, lookup, "Store A", "Groceries");

    expect(label).toBe("Groceries → Store A");
  });

  it("falls back to a generic marker when archived and no stored name is available", () => {
    const lookup = new Map<number, CategoryRead>();

    const label = categoryLabel(2, lookup, null, null);

    expect(label).toBe("Archived category");
  });

  it("returns Uncategorized when there is no id at all", () => {
    const lookup = new Map<number, CategoryRead>();

    expect(categoryLabel(null, lookup, "irrelevant", "irrelevant")).toBe("Uncategorized");
    expect(categoryLabel(undefined, lookup)).toBe("Uncategorized");
  });
});
