import { describe, expect, it } from "vitest";
import { periodKey, periodParams, periodRange } from "@/lib/period";

describe("periodKey", () => {
  it("renders a month period as YYYY-MM", () => {
    expect(periodKey({ year: 2026, mon: 6 })).toBe("2026-06");
  });

  it("zero-pads single-digit months", () => {
    expect(periodKey({ year: 2026, mon: 1 })).toBe("2026-01");
  });

  it("renders a year period (no mon) as YYYY", () => {
    expect(periodKey({ year: 2026 })).toBe("2026");
  });
});

describe("periodRange", () => {
  it("returns Jan 1 - Dec 31 for a year period", () => {
    expect(periodRange({ year: 2026 })).toEqual({
      start: "2026-01-01",
      end: "2026-12-31",
    });
  });

  // Date.UTC(year, mon, 0) walks back one day from the 1st of `mon` (0-indexed
  // month `mon` here IS next month, since `p.mon` is 1-indexed) to land on the
  // last day of the target month. Leap-year February is the sharpest edge:
  // getting the leap rule wrong is invisible in every other month.
  it("computes lastDay = 29 for February in a leap year", () => {
    expect(periodRange({ year: 2024, mon: 2 })).toEqual({
      start: "2024-02-01",
      end: "2024-02-29",
    });
  });

  it("computes lastDay = 28 for February in a non-leap year", () => {
    expect(periodRange({ year: 2025, mon: 2 })).toEqual({
      start: "2025-02-01",
      end: "2025-02-28",
    });
  });

  // December is the other edge worth pinning: `Date.UTC(year, 12, 0)` rolls
  // into "month 12" of the JS Date (which normalizes to January of the next
  // calendar year internally) before stepping back a day — an easy place for
  // an off-by-one to hide since there's no 13th month to overflow into.
  it("computes lastDay = 31 for December", () => {
    expect(periodRange({ year: 2025, mon: 12 })).toEqual({
      start: "2025-12-01",
      end: "2025-12-31",
    });
  });
});

describe("periodParams", () => {
  it("emits only {month} for a month period, never {year}", () => {
    const params = periodParams({ year: 2026, mon: 6 });
    expect(params).toEqual({ month: "2026-06" });
    expect(params.year).toBeUndefined();
  });

  it("emits only {year} for a year period, never {month}", () => {
    const params = periodParams({ year: 2026 });
    expect(params).toEqual({ year: "2026" });
    expect(params.month).toBeUndefined();
  });
});
