import { describe, expect, it, vi } from "vitest";
import type { CategoryRead } from "@/lib/api/client";
import { CategorySelector } from "@/components/categories/category-selector";
import { renderWithProviders, screen } from "@/tests/helpers/test-utils";

const mockCategories: CategoryRead[] = [
  {
    id: 1,
    name: "Food & Dining",
    kind: "spend",
    is_seeded: true,
    archived_at: null,
    color: "#ff5500",
    display_color: "#ff5500",
    parent_id: null,
    role: null,
  },
  {
    id: 2,
    name: "Groceries",
    kind: "spend",
    is_seeded: true,
    archived_at: null,
    color: null,
    display_color: "#ff5500",
    parent_id: 1,
    role: null,
  },
  {
    id: 3,
    name: "Restaurants",
    kind: "spend",
    is_seeded: true,
    archived_at: null,
    color: null,
    display_color: "#ff5500",
    parent_id: 1,
    role: null,
  },
  {
    id: 5,
    name: "Food",
    kind: "spend",
    is_seeded: true,
    archived_at: null,
    color: null,
    display_color: "#ff5500",
    parent_id: 1,
    role: null,
  },
  {
    id: 6,
    name: "Fast Food",
    kind: "spend",
    is_seeded: true,
    archived_at: null,
    color: null,
    display_color: "#ff5500",
    parent_id: 1,
    role: null,
  },
  {
    id: 4,
    name: "Salary",
    kind: "income",
    is_seeded: true,
    archived_at: null,
    color: "#00aa00",
    display_color: "#00aa00",
    parent_id: null,
    role: null,
  },
];

describe("CategorySelector", () => {
  it("renders placeholder when no category is selected", () => {
    renderWithProviders(
      <CategorySelector
        value={null}
        onChange={vi.fn()}
        categories={mockCategories}
        placeholder="Select category"
      />,
    );

    expect(screen.getByRole("button")).toHaveTextContent("Select category");
  });

  it("renders selected category name with parent hierarchy rollup", () => {
    renderWithProviders(
      <CategorySelector
        value={2} // Groceries (sub of Food & Dining)
        onChange={vi.fn()}
        categories={mockCategories}
      />,
    );

    expect(screen.getByRole("button")).toHaveTextContent(
      "Food & Dining → Groceries",
    );
  });

  it("opens popover and lists categories filtered by kind", async () => {
    const handleChange = vi.fn();
    const { user } = renderWithProviders(
      <CategorySelector
        value={null}
        onChange={handleChange}
        categories={mockCategories}
        kind="spend"
      />,
    );

    // Click trigger to open dropdown
    await user.click(screen.getByRole("button"));

    // Parent category and subcategories should be visible
    expect(screen.getByText("Food & Dining")).toBeInTheDocument();
    expect(screen.getByText("Groceries")).toBeInTheDocument();
    expect(screen.getByText("Restaurants")).toBeInTheDocument();
    // Income category "Salary" should be excluded because kind="spend"
    expect(screen.queryByText("Salary")).not.toBeInTheDocument();
  });

  it("calls onChange with category ID when an item is selected", async () => {
    const handleChange = vi.fn();
    const { user } = renderWithProviders(
      <CategorySelector
        value={null}
        onChange={handleChange}
        categories={mockCategories}
      />,
    );

    await user.click(screen.getByRole("button"));
    await user.click(screen.getByText("Groceries"));

    expect(handleChange).toHaveBeenCalledWith(2);
  });

  it("ranks exact match first when searching", async () => {
    const { user } = renderWithProviders(
      <CategorySelector
        value={null}
        onChange={vi.fn()}
        categories={mockCategories}
      />,
    );

    await user.click(screen.getByRole("button"));

    const searchInput = screen.getByPlaceholderText("Search categories…");
    await user.type(searchInput, "Food");

    // All matching items should be present
    const items = screen.getAllByRole("option");
    // #1 item must be the exact match "Food" (id: 5)
    expect(items[0]).toHaveTextContent(/^Food\b/);
    // Followed by prefix match "Food & Dining"
    expect(items[1]).toHaveTextContent(/^Food & Dining/);
    // Followed by word boundary match "Fast Food"
    expect(items[2]).toHaveTextContent(/^Fast Food/);
  });

  it("renders empty state when no category matches search query", async () => {
    const { user } = renderWithProviders(
      <CategorySelector
        value={null}
        onChange={vi.fn()}
        categories={mockCategories}
      />,
    );

    await user.click(screen.getByRole("button"));

    const searchInput = screen.getByPlaceholderText("Search categories…");
    await user.type(searchInput, "xyznonexistent");

    expect(screen.getByText("No matching categories.")).toBeInTheDocument();
  });

  it("ranks direct match above match via parent name", async () => {
    const categoriesWithDining: CategoryRead[] = [
      ...mockCategories,
      {
        id: 7,
        name: "Dining Supplies",
        kind: "spend",
        is_seeded: true,
        archived_at: null,
        color: null,
        display_color: "#ff5500",
        parent_id: null,
        role: null,
      },
    ];

    const { user } = renderWithProviders(
      <CategorySelector
        value={null}
        onChange={vi.fn()}
        categories={categoriesWithDining}
      />,
    );

    await user.click(screen.getByRole("button"));

    const searchInput = screen.getByPlaceholderText("Search categories…");
    await user.type(searchInput, "Dining");

    const items = screen.getAllByRole("option");
    // "Dining Supplies" directly starts with "Dining" (score 2)
    // "Food & Dining" contains "Dining" (score 3 or 4)
    // "Groceries" only matches because parent is "Food & Dining" (score 5)
    const texts = items.map((el) => el.textContent || "");
    const diningDirectIdx = texts.findIndex((t) => t.includes("Dining Supplies"));
    const parentMatchIdx = texts.findIndex((t) => t.includes("Groceries"));

    expect(diningDirectIdx).toBeGreaterThanOrEqual(0);
    expect(parentMatchIdx).toBeGreaterThanOrEqual(0);
    expect(diningDirectIdx).toBeLessThan(parentMatchIdx);
  });
});
