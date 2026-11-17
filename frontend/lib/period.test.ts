import { describe, expect, it } from "vitest";
import {
  currentYear,
  fyOf,
  periodKey,
  periodLabel,
  periodParams,
  periodRange,
  previousPeriod,
  taxYearLabel,
} from "@/lib/period";

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
  // PRD §F8: a spend year is the calendar year, 1 Jan - 31 Dec — the same span the
  // backend's dashboards `?year=` covers.
  it("returns 1 Jan - 31 Dec for a year period (the calendar year)", () => {
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

describe("periodLabel", () => {
  it("labels a year period as the bare calendar year", () => {
    expect(periodLabel({ year: 2026 })).toBe("2026");
  });

  it("labels a month period as the calendar month", () => {
    expect(periodLabel({ year: 2026, mon: 6 })).toBe("June 2026");
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

describe("currentYear", () => {
  it("is the calendar year of the local date, January included", () => {
    expect(currentYear(new Date(2027, 0, 1))).toBe(2027);
    expect(currentYear(new Date(2026, 11, 31))).toBe(2026);
  });
});

// Tax only (PRD §F11): the tax pages still name the FY by its start year.
describe("fyOf", () => {
  it("puts January–March in the financial year that began the previous April", () => {
    expect(fyOf({ year: 2027, mon: 2 })).toBe(2026);
    expect(fyOf({ year: 2027, mon: 3 })).toBe(2026);
    expect(fyOf({ year: 2026, mon: 4 })).toBe(2026);
    expect(fyOf({ year: 2026, mon: 12 })).toBe(2026);
    // A year period already names its FY.
    expect(fyOf({ year: 2026 })).toBe(2026);
  });
});

describe("previousPeriod", () => {
  it("a year's previous period is the previous calendar year", () => {
    expect(previousPeriod({ year: 2026 })).toEqual({ year: 2025 });
    expect(periodRange(previousPeriod({ year: 2026 }))).toEqual({
      start: "2025-01-01",
      end: "2025-12-31",
    });
  });

  it("January's previous period is the previous December", () => {
    expect(previousPeriod({ year: 2027, mon: 1 })).toEqual({ year: 2026, mon: 12 });
    expect(previousPeriod({ year: 2026, mon: 6 })).toEqual({ year: 2026, mon: 5 });
  });
});

describe("taxYearLabel", () => {
  it("labels tax years from 2026-27 on, and earlier years by FY and AY", () => {
    expect(taxYearLabel(2026)).toBe("Tax year 2026-27");
    expect(taxYearLabel(2027)).toBe("Tax year 2027-28");
    expect(taxYearLabel(2025)).toBe("FY 2025-26 (AY 2026-27)");
  });
});
