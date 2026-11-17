import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { SpendByCategory } from "./spend-by-category";
import { renderWithProviders, screen } from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";
import type { SpendByCategoryRow } from "@/lib/api/client";
import { buildCategory } from "@/tests/helpers/factories";

function row(id: number, name: string, total: number): SpendByCategoryRow {
  return {
    category_id: id,
    category_name: name,
    parent_id: null,
    parent_name: null,
    display_color: "#2a78d6",
    parent_display_color: null,
    total_paise: total,
  };
}

describe("SpendByCategory (components/dashboard/spend-by-category.tsx)", () => {
  it("compares a calendar year with the previous year (PRD §F8)", async () => {
    server.use(
      http.get("*/api/v1/dashboards/spend-by-category", ({ request }) => {
        const url = new URL(request.url);
        const year = url.searchParams.get("year");
        // 2026 spent 200; 2025 spent 100.
        const total = year === "2026" ? -20000 : -10000;
        return HttpResponse.json({
          period: year,
          rows: [row(1, "Food & Dining", total)],
          label_id: null,
        });
      }),
    );

    renderWithProviders(<SpendByCategory year={2026} />);

    // Spend doubled against the previous year: a chip titled with that year.
    const chip = await screen.findByTitle("vs 2025");
    expect(chip).toHaveTextContent("100");
  });

  it("draws view 2 in the rows' display colours, an archived subcategory included", async () => {
    const child = (id: number, name: string, color: string | null) =>
      buildCategory({
        id,
        name,
        parent_id: 1,
        color,
        display_color: color ?? "#d55181",
      });
    server.use(
      http.get("*/api/v1/categories", () =>
        HttpResponse.json([
          buildCategory({ id: 1, name: "Shopping", color: "#d55181" }),
          child(2, "Clothing", null),
          child(3, "Gadgets", "#0e9488"),
        ]),
      ),
      http.get("*/api/v1/dashboards/spend-by-category", () => {
        const sub = (
          id: number,
          name: string,
          display_color: string,
          total: number,
        ): SpendByCategoryRow => ({
          ...row(id, name, total),
          parent_id: 1,
          parent_name: "Shopping",
          display_color,
          parent_display_color: "#d55181",
        });
        return HttpResponse.json({
          period: "2026",
          rows: [
            sub(2, "Clothing", "#d55181", -30000),
            sub(3, "Gadgets", "#0e9488", -20000),
            sub(4, "Old shoes", "#d55181", -10000),
          ],
          label_id: null,
        });
      }),
    );

    const { user } = renderWithProviders(<SpendByCategory year={2026} />);

    const barOf = async (name: string | RegExp) =>
      (await screen.findByText(name)).closest("li")!.querySelector("div[style]");

    expect(await barOf("Shopping")).toHaveStyle({ backgroundColor: "#d55181" });

    await user.click(await screen.findByTitle("Click to drill down into subcategories"));
    expect(await barOf("Clothing")).toHaveStyle({ backgroundColor: "#d55181" });
    expect(await barOf("Gadgets")).toHaveStyle({ backgroundColor: "#0e9488" });
    expect(await barOf("Old shoes")).toHaveStyle({ backgroundColor: "#d55181" });

    await user.click(screen.getByTitle("Back to parent categories"));
    await user.click(screen.getByRole("button", { name: "All Detailed" }));
    expect(await barOf(/Gadgets/)).toHaveStyle({ backgroundColor: "#0e9488" });
    expect(await barOf(/Clothing/)).toHaveStyle({ backgroundColor: "#d55181" });
  });
});
