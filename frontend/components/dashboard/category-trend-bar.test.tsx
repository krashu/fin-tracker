import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { CategoryTrendBar, TrendTooltipRow } from "./category-trend-bar";
import { renderWithProviders, screen, within } from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";
import type {
  CategoryRead,
  CategoryTrendParent,
  CategoryTrendResponse,
} from "@/lib/api/client";
import { formatINR } from "@/lib/format";
import type { TrendSeries } from "@/lib/category-trend";

const PERIODS = ["2026-01", "2026-02", "2026-03"];

function parent(
  id: number | null,
  name: string | null,
  per: number,
  extra: Partial<CategoryTrendParent> = {},
): CategoryTrendParent {
  return {
    parent_id: id,
    parent_name: name,
    color: id == null ? null : "#2a78d6",
    archived: false,
    totals_paise: PERIODS.map(() => -per),
    subcategories: [],
    ...extra,
  };
}

function cat(id: number, name: string, parentId: number | null): CategoryRead {
  return {
    id,
    name,
    kind: "spend",
    is_seeded: false,
    archived_at: null,
    color: parentId == null ? "#2a78d6" : null,
    display_color: "#2a78d6",
    parent_id: parentId,
    role: null,
  };
}

const HOUSEHOLD = parent(1, "Household", 9000, {
  subcategories: [
    {
      category_id: 2,
      category_name: "Supplies",
      color: "#008300",
      archived: false,
      is_direct: false,
      totals_paise: PERIODS.map(() => -5000),
    },
    {
      category_id: 3,
      category_name: "Repairs",
      color: "#d55181",
      archived: true,
      is_direct: false,
      totals_paise: PERIODS.map(() => -3000),
    },
    {
      category_id: 1,
      category_name: "Household",
      color: "#2a78d6",
      archived: false,
      is_direct: true,
      totals_paise: PERIODS.map(() => -1000),
    },
  ],
});

function stub(
  body: CategoryTrendResponse,
  seen: URLSearchParams[] = [],
  categories: CategoryRead[] = [],
) {
  server.use(
    http.get("*/api/v1/dashboards/category-trend", ({ request }) => {
      seen.push(new URL(request.url).searchParams);
      return HttpResponse.json(body);
    }),
    http.get("*/api/v1/categories", () => HttpResponse.json(categories)),
  );
}

describe("CategoryTrendBar", () => {
  it("requests 12 months with no tag and lists six parents then the fold and uncategorized", async () => {
    const parents = [
      ...Array.from({ length: 9 }, (_, k) => parent(k + 1, `Parent${k + 1}`, (k + 1) * 1000)),
      parent(null, null, 400),
    ];
    const seen: URLSearchParams[] = [];
    stub({ periods: PERIODS, parents, label_id: null }, seen);

    renderWithProviders(<CategoryTrendBar />);

    expect(await screen.findByText("3 more categories")).toBeInTheDocument();
    expect(seen[0].get("months")).toBe("12");
    expect(seen[0].has("label_id")).toBe(false);
    const legendButtons = screen.getAllByRole("button", { name: /^Show .* subcategories$/ });
    expect(legendButtons).toHaveLength(6);
    expect(screen.getByText("Uncategorized").closest("button")).toBeNull();
    expect(screen.getByText("3 more categories").closest("button")).toBeNull();
  });

  it("names the series and the amount in a tooltip row", () => {
    const series: TrendSeries = {
      key: "c1",
      label: "Household",
      color: "#2a78d6",
      pattern: "solid",
      kind: "category",
      categoryId: 1,
      drillable: true,
      totalsPaise: [],
    };
    renderWithProviders(<TrendTooltipRow series={series} value={123456} />);
    expect(screen.getByText("Household")).toBeInTheDocument();
    expect(screen.getByText(formatINR(123456))).toBeInTheDocument();
  });

  it("drills into a parent from the legend and returns focus to it on back", async () => {
    stub(
      { periods: PERIODS, parents: [HOUSEHOLD, parent(4, "Trips", 1000)], label_id: null },
      [],
      [cat(1, "Household", null), cat(2, "Supplies", 1)],
    );
    const { user } = renderWithProviders(<CategoryTrendBar />);

    await user.click(await screen.findByRole("button", { name: "Show Household subcategories" }));

    expect(await screen.findByRole("button", { name: "← All categories" })).toHaveFocus();
    const legend = screen.getByRole("list");
    expect(within(legend).getByText("Supplies")).toBeInTheDocument();
    expect(within(legend).getByText("Repairs (archived)")).toBeInTheDocument();
    expect(within(legend).getByText("Household (direct)")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "← All categories" }));
    expect(
      await screen.findByRole("button", { name: "Show Household subcategories" }),
    ).toHaveFocus();
  });

  it("reaches a folded parent through the table and returns focus to its row button", async () => {
    const parents = Array.from({ length: 9 }, (_, k) =>
      parent(k + 1, `Parent${k + 1}`, (k + 1) * 1000),
    );
    stub({ periods: PERIODS, parents, label_id: null });
    const { user } = renderWithProviders(<CategoryTrendBar />);

    await user.click(await screen.findByRole("button", { name: "Table" }));
    // Parent1 spends least: rank 9, folded out of the chart but listed here.
    const rowButton = await screen.findByRole("button", { name: "Parent1" });
    await user.click(rowButton);

    expect(await screen.findByRole("button", { name: "← All categories" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "← All categories" }));

    const back = await screen.findByRole("button", { name: "Parent1" });
    expect(back).toHaveFocus();
    expect(back).toHaveAttribute("data-drill-source", "table");
  });

  it("masks the chart and table amounts by default", async () => {
    stub({ periods: PERIODS, parents: [parent(1, "Household", 9000)], label_id: null });
    const { user } = renderWithProviders(<CategoryTrendBar />);

    expect((await screen.findByTestId("trend-chart")).className).toContain("blur-sm");
    await user.click(screen.getByRole("button", { name: "Table" }));
    expect(screen.getAllByLabelText("Amount hidden").length).toBeGreaterThan(0);
    expect(screen.queryByText(formatINR(9000))).not.toBeInTheDocument();
  });

  it("shows the unscoped empty copy, and the tagged one under a tag", async () => {
    stub({ periods: PERIODS, parents: [], label_id: null });
    const first = renderWithProviders(<CategoryTrendBar />);
    expect(await screen.findByText("No spending in the last 12 months.")).toBeInTheDocument();
    first.unmount();

    stub({ periods: PERIODS, parents: [], label_id: 5 });
    renderWithProviders(<CategoryTrendBar labelId={5} />);
    expect(await screen.findByText("No tagged spending in the last 12 months.")).toBeInTheDocument();
  });

  it("shows the error copy when the request fails", async () => {
    server.use(
      http.get("*/api/v1/dashboards/category-trend", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
      http.get("*/api/v1/categories", () => HttpResponse.json([])),
    );
    renderWithProviders(<CategoryTrendBar />);
    expect(await screen.findByText(/Couldn’t load/)).toBeInTheDocument();
  });
});
