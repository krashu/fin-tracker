import { describe, expect, it } from "vitest";
import { yearBoundaries } from "@/lib/charts";

function months(start: string, n: number): string[] {
  const [y, m] = start.split("-").map(Number);
  return Array.from({ length: n }, (_, i) => {
    const idx = y * 12 + (m - 1) + i;
    return `${Math.floor(idx / 12)}-${String((idx % 12) + 1).padStart(2, "0")}`;
  });
}

describe("yearBoundaries", () => {
  it("finds the one January inside a 12-month window", () => {
    expect(yearBoundaries(months("2025-06", 12))).toEqual([
      { period: "2026-01", label: "2026" },
    ]);
  });

  it("includes a January at the left edge", () => {
    expect(yearBoundaries(months("2026-01", 12))).toEqual([
      { period: "2026-01", label: "2026" },
    ]);
  });

  it("finds two in a 24-month window", () => {
    expect(yearBoundaries(months("2025-01", 24))).toEqual([
      { period: "2025-01", label: "2025" },
      { period: "2026-01", label: "2026" },
    ]);
  });

  it("returns nothing when no January is in range", () => {
    expect(yearBoundaries(["2026-06", "2026-07", "2026-08"])).toEqual([]);
  });
});
