import { describe, expect, it } from "vitest";
import {
  monthKey,
  monthRange,
  thisMonthAnchor,
  toLocalYMD,
  trailingMonths,
  trailingWeeksWindow,
  yearRange,
} from "@/lib/dates";

describe("toLocalYMD", () => {
  it("formats Date as YYYY-MM-DD using local time components", () => {
    const d = new Date(2026, 0, 5); // Jan 5, 2026
    expect(toLocalYMD(d)).toBe("2026-01-05");
    const d2 = new Date(2026, 11, 31); // Dec 31, 2026
    expect(toLocalYMD(d2)).toBe("2026-12-31");
  });
});

describe("thisMonthAnchor & monthKey", () => {
  it("creates a first-of-month anchor for current month", () => {
    const anchor = thisMonthAnchor();
    expect(anchor.getDate()).toBe(1);
    expect(monthKey(anchor)).toMatch(/^\d{4}-\d{2}$/);
  });

  it("formats monthKey as YYYY-MM", () => {
    const d = new Date(2026, 7, 15); // Aug 15, 2026
    expect(monthKey(d)).toBe("2026-08");
  });
});

describe("monthRange", () => {
  it("computes first and last day of the calendar month", () => {
    const febNonLeap = new Date(2025, 1, 10);
    expect(monthRange(febNonLeap)).toEqual({
      date_from: "2025-02-01",
      date_to: "2025-02-28",
    });

    const febLeap = new Date(2024, 1, 10);
    expect(monthRange(febLeap)).toEqual({
      date_from: "2024-02-01",
      date_to: "2024-02-29",
    });

    const dec = new Date(2026, 11, 1);
    expect(monthRange(dec)).toEqual({
      date_from: "2026-12-01",
      date_to: "2026-12-31",
    });
  });
});

describe("yearRange", () => {
  it("computes Jan 1 to Dec 31 for the given year", () => {
    expect(yearRange(2026)).toEqual({
      date_from: "2026-01-01",
      date_to: "2026-12-31",
    });
  });
});

describe("trailingWeeksWindow", () => {
  it("aligns to Monday for weeksBack weeks ago to today", () => {
    // 2026-06-17 is Wednesday (getDay() = 3)
    const wednesday = new Date(2026, 5, 17);
    const window = trailingWeeksWindow(wednesday, 2);

    expect(window.end).toBe("2026-06-17");
    // Monday of that week is 2026-06-15. 2 weeks back is 2026-06-01.
    expect(window.start).toBe("2026-06-01");
  });
});

describe("trailingMonths", () => {
  it("computes first-of-month monthsBack ago to today", () => {
    const midAugust = new Date(2026, 7, 15);
    const window = trailingMonths(midAugust, 3);

    expect(window.end).toBe("2026-08-15");
    // 3 months back first of month: 2026-05-01
    expect(window.start).toBe("2026-05-01");
  });
});
