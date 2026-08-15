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
    parent_id: null,
  },
  {
    id: 2,
    name: "Groceries",
    kind: "spend",
    is_seeded: true,
    archived_at: null,
    color: null,
    parent_id: 1,
  },
  {
    id: 3,
    name: "Restaurants",
    kind: "spend",
    is_seeded: true,
    archived_at: null,
    color: null,
    parent_id: 1,
  },
  {
    id: 4,
    name: "Salary",
    kind: "income",
    is_seeded: true,
    archived_at: null,
    color: "#00aa00",
    parent_id: null,
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
});
