import { describe, expect, it } from "vitest";
import type {
  CategoryTrendParent,
  CategoryTrendResponse,
  CategoryTrendSubcategory,
} from "@/lib/api/client";
import {
  chartRows,
  drillSeries,
  overviewSeries,
  type TrendSeries,
} from "@/lib/category-trend";

const PERIODS = ["2026-01", "2026-02", "2026-03"];

function parent(
  id: number | null,
  name: string | null,
  color: string | null,
  totals: number[],
  extra: Partial<CategoryTrendParent> = {},
): CategoryTrendParent {
  return {
    parent_id: id,
    parent_name: name,
    color,
    archived: false,
    totals_paise: totals,
    subcategories: [],
    ...extra,
  };
}

function sub(
  id: number,
  name: string,
  totals: number[],
  extra: Partial<CategoryTrendSubcategory> = {},
): CategoryTrendSubcategory {
  return {
    category_id: id,
    category_name: name,
    color: "#008300",
    archived: false,
    is_direct: false,
    totals_paise: totals,
    ...extra,
  };
}

const HUES = [
  "#2a78d6",
  "#008300",
  "#d55181",
  "#c98500",
  "#1baf7a",
  "#d95926",
  "#6c5cd6",
  "#e34948",
  "#94a3b8",
];

/** 9 categorized parents, id i spending i * 1000 in each month (so id 9 is
 * rank 1), plus uncategorized. Spend is negative on the wire. */
function nineParents(): CategoryTrendResponse {
  const parents = Array.from({ length: 9 }, (_, k) => {
    const id = k + 1;
    return parent(id, `P${id}`, HUES[k], PERIODS.map(() => -id * 1000));
  });
  parents.push(parent(null, null, null, [-500, -600, -700]));
  return { periods: PERIODS, parents, label_id: null };
}

function sumSeries(series: TrendSeries[], i: number): number {
  return series.reduce((acc, s) => acc + s.totalsPaise[i], 0);
}

describe("overviewSeries", () => {
  it("shows the top six by spend, hatches the rest into a fold, keeps uncategorized last", () => {
    const resp = nineParents();
    const series = overviewSeries(resp);

    expect(series.map((s) => s.label)).toEqual([
      "P9",
      "P8",
      "P7",
      "P6",
      "P5",
      "P4",
      "3 more categories",
      "Uncategorized",
    ]);
    const fold = series[6];
    expect(fold.kind).toBe("fold");
    expect(fold.pattern).toBe("hatch");
    expect(fold.drillable).toBe(false);
    expect(fold.totalsPaise).toEqual([-6000, -6000, -6000]); // P1+P2+P3
    const uncat = series[7];
    expect(uncat.kind).toBe("uncategorized");
    expect(uncat.color).toBe("var(--chart-uncategorized)");
    expect(uncat.drillable).toBe(false);

    for (let i = 0; i < PERIODS.length; i++) {
      const parentsSum = resp.parents.reduce((a, p) => a + p.totals_paise[i], 0);
      expect(sumSeries(series, i)).toBe(parentsSum);
    }
  });

  it("does not fold when there are exactly seven categorized parents", () => {
    const resp = nineParents();
    resp.parents = resp.parents.filter((p) => p.parent_id == null || p.parent_id <= 7);
    const series = overviewSeries(resp);
    expect(series.filter((s) => s.kind === "category")).toHaveLength(7);
    expect(series.some((s) => s.kind === "fold")).toBe(false);
  });

  it("gives every series a distinct (colour, pattern) pair", () => {
    const series = overviewSeries(nineParents());
    const pairs = series.map((s) => `${s.color}|${s.pattern}`);
    expect(new Set(pairs).size).toBe(pairs.length);
  });

  it("keeps a parent's colour when its rank changes", () => {
    const before = overviewSeries(nineParents());
    const resp = nineParents();
    // P3 jumps from rank 7 (folded) to rank 1.
    resp.parents[2].totals_paise = [-99000, -99000, -99000];
    const after = overviewSeries(resp);
    expect(after[0].label).toBe("P3");
    const p5Before = before.find((s) => s.label === "P5");
    const p5After = after.find((s) => s.label === "P5");
    expect(p5After?.color).toBe(p5Before?.color);
    expect(p5Before?.color).toBe(HUES[4]);
  });

  it("marks an archived parent", () => {
    const resp = nineParents();
    resp.parents[8].archived = true; // P9
    expect(overviewSeries(resp)[0].label).toBe("P9 (archived)");
  });

  it("ranks a net-credit parent last and keeps its sign", () => {
    const resp = nineParents();
    resp.parents[0].totals_paise = [4000, 4000, 4000]; // P1 is a net credit
    const series = overviewSeries(resp);
    // Credit sorts after every spending parent; it lands in the fold.
    expect(series.some((s) => s.label === "P1")).toBe(false);
    // Fold = P3 (-3000) + P2 (-2000) + P1 (+4000).
    expect(series.find((s) => s.kind === "fold")?.totalsPaise).toEqual([
      -1000, -1000, -1000,
    ]);
  });
});

describe("drillSeries", () => {
  const parentHue = "#d95926";
  function householdParent(): CategoryTrendParent {
    const subs = [
      ...[2, 3, 4, 5, 6].map((id) => sub(id, `Sub${id}`, [-id * 100, 0, 0])),
      sub(7, "Repairs", [-50, 0, 0], { archived: true }),
      sub(1, "Household", [-10, 0, 0], { is_direct: true }),
    ];
    return parent(1, "Household", parentHue, [-2150 - 50 - 10 + 0, 0, 0], {
      subcategories: subs,
    });
  }

  it("lists subs by spend, the archived one marked, the direct row last", () => {
    const series = drillSeries(householdParent());
    expect(series.map((s) => s.label)).toEqual([
      "Sub6",
      "Sub5",
      "Sub4",
      "Sub3",
      "Sub2",
      "Repairs (archived)",
      "Household (direct)",
    ]);
    expect(series.at(-1)?.kind).toBe("direct");
    const p = householdParent();
    for (let i = 0; i < 3; i++) {
      expect(sumSeries(series, i)).toBe(
        p.subcategories.reduce((a, s) => a + s.totals_paise[i], 0),
      );
    }
  });

  it("colours every drill series with the colour the server sent", () => {
    const hexes = ["#008300", "#d55181", "#c98500", "#1baf7a", "#d95926", "#6c5cd6", "#e34948"];
    const subs = [
      ...[2, 3, 4, 5, 6].map((id, k) => sub(id, `Sub${id}`, [-id * 100, 0, 0], { color: hexes[k] })),
      sub(7, "Repairs", [-50, 0, 0], { archived: true, color: hexes[5] }),
      sub(1, "Household", [-10, 0, 0], { is_direct: true, color: parentHue }),
    ];
    const p = parent(1, "Household", parentHue, [-2150 - 50 - 10, 0, 0], { subcategories: subs });
    const series = drillSeries(p);
    const colorOfLabel = new Map(series.map((s) => [s.label, s.color]));
    for (const s of subs.filter((x) => !x.is_direct)) {
      const label = s.archived ? `${s.category_name} (archived)` : s.category_name;
      expect(colorOfLabel.get(label ?? "")).toBe(s.color);
    }
    expect(series.at(-1)?.color).toBe(parentHue);
  });

  it("folds subcategories past the top six into one hatched series, direct row still last", () => {
    const subs = [
      ...Array.from({ length: 9 }, (_, k) => sub(10 + k, `S${k}`, [-(k + 1) * 100, 0, 0])),
      sub(1, "Household", [-5, 0, 0], { is_direct: true }),
    ];
    const p = parent(1, "Household", parentHue, [-4505, 0, 0], { subcategories: subs });
    const series = drillSeries(p);
    expect(series.map((s) => s.label)).toEqual([
      "S8",
      "S7",
      "S6",
      "S5",
      "S4",
      "S3",
      "3 more subcategories",
      "Household (direct)",
    ]);
    expect(series[6].pattern).toBe("hatch");
    expect(series[6].totalsPaise[0]).toBe(-(100 + 200 + 300));
  });
});

describe("drillSeries fold threshold", () => {
  it("does not fold exactly seven subcategories", () => {
    const subs = Array.from({ length: 7 }, (_, k) =>
      sub(10 + k, `S${k}`, [-(k + 1) * 100, 0, 0]),
    );
    const p = parent(1, "Household", "#d95926", [-2800, 0, 0], { subcategories: subs });
    const series = drillSeries(p);
    expect(series).toHaveLength(7);
    expect(series.some((s) => s.kind === "fold")).toBe(false);
  });
});

describe("chartRows", () => {
  it("plots spend positive: each value is the negated paise", () => {
    const series: TrendSeries[] = [
      {
        key: "c1",
        label: "A",
        color: "#000",
        pattern: "solid",
        kind: "category",
        categoryId: 1,
        drillable: true,
        totalsPaise: [-100, 250, 0],
      },
    ];
    expect(chartRows(PERIODS, series)).toEqual([
      { period: "2026-01", c1: 100 },
      { period: "2026-02", c1: -250 },
      { period: "2026-03", c1: 0 },
    ]);
  });
});
