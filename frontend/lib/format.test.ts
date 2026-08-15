import { describe, expect, it, vi } from "vitest";
import {
  compactINR,
  formatDate,
  formatDateRange,
  formatDateWithYear,
  formatDecimalMoney,
  formatINR,
  formatINRWhole,
  formatMoney,
  formatMonth,
  formatMonthYear,
  formatPercent,
  formatUnits,
  paiseToRupees,
  rupeesToPaise,
} from "@/lib/format";

describe("formatINR", () => {
  it("formats positive paise with Indian groupings and 2 decimals", () => {
    // 12345678 paise = ₹1,23,456.78
    expect(formatINR(12345678)).toMatch(/₹\s?1,23,456\.78/);
    expect(formatINR(100)).toMatch(/₹\s?1\.00/);
    expect(formatINR(50)).toMatch(/₹\s?0\.50/);
  });

  it("formats negative paise properly", () => {
    expect(formatINR(-50000)).toMatch(/-₹\s?500\.00/);
  });

  it("drops the negative sign for negative zero", () => {
    expect(formatINR(0)).toMatch(/₹\s?0\.00/);
    expect(formatINR(-0)).toMatch(/₹\s?0\.00/);
  });
});

describe("formatINRWhole", () => {
  it("formats paise without decimals", () => {
    expect(formatINRWhole(12345678)).toMatch(/₹\s?1,23,457/);
    expect(formatINRWhole(50000)).toMatch(/₹\s?500/);
    expect(formatINRWhole(0)).toMatch(/₹\s?0/);
  });
});

describe("compactINR", () => {
  it("formats numbers in Crores (>= 1e7 rupees / 1e9 paise)", () => {
    expect(compactINR(1.2e9)).toBe("₹1.2Cr");
    expect(compactINR(2.6e9)).toBe("₹2.6Cr");
    expect(compactINR(-1.5e9)).toBe("-₹1.5Cr");
  });

  it("formats numbers in Lakhs (>= 1e5 rupees / 1e7 paise)", () => {
    expect(compactINR(3.4e7)).toBe("₹3.4L");
    expect(compactINR(1e7)).toBe("₹1.0L");
    expect(compactINR(-2.5e7)).toBe("-₹2.5L");
  });

  it("formats numbers in thousands (>= 1e3 rupees / 1e5 paise)", () => {
    expect(compactINR(250000)).toBe("₹2.5k");
    // Trailing .0 is trimmed
    expect(compactINR(300000)).toBe("₹3k");
    expect(compactINR(-1060000)).toBe("-₹10.6k");
  });

  it("formats smaller numbers as whole rupees without suffix", () => {
    expect(compactINR(50000)).toBe("₹500");
    expect(compactINR(9900)).toBe("₹99");
    expect(compactINR(0)).toBe("₹0");
    // Sub-50 paise negative amount rounds to zero and drops sign
    expect(compactINR(-30)).toBe("₹0");
  });
});

describe("rupeesToPaise and paiseToRupees", () => {
  it("converts user-typed rupee string to integer paise", () => {
    expect(rupeesToPaise("1234.50")).toBe(123450);
    expect(rupeesToPaise("0.5")).toBe(50);
    expect(rupeesToPaise("100")).toBe(10000);
    expect(rupeesToPaise("")).toBe(0);
    expect(rupeesToPaise("abc")).toBe(0);
  });

  it("converts paise to exact 2-decimal rupee string", () => {
    expect(paiseToRupees(123450)).toBe("1234.50");
    expect(paiseToRupees(50)).toBe("0.50");
    expect(paiseToRupees(0)).toBe("0.00");
  });
});

describe("formatMoney & formatDecimalMoney", () => {
  it("formats INR and USD with respective symbols", () => {
    expect(formatMoney(10000, "INR")).toMatch(/₹\s?100\.00/);
    expect(formatMoney(10000, "USD")).toMatch(/\$\s?100\.00/);
  });

  it("formats decimal string money values", () => {
    expect(formatDecimalMoney("123.456", "USD")).toMatch(/\$\s?123\.46/);
    expect(formatDecimalMoney("invalid", "INR")).toBe("invalid");
  });
});

describe("formatUnits", () => {
  it("formats unit quantities and trims precision", () => {
    expect(formatUnits("12.345678", 4)).toBe("12.3457");
    expect(formatUnits("10.000", 2)).toBe("10");
    expect(formatUnits("invalid")).toBe("invalid");
  });
});

describe("formatPercent", () => {
  it("formats fractions as percentages with 1 decimal place", () => {
    expect(formatPercent(0.623)).toBe("62.3%");
    expect(formatPercent(0.05)).toBe("5.0%");
    expect(formatPercent(1)).toBe("100.0%");
  });
});

describe("Date formatters", () => {
  it("formats month and year strings", () => {
    const d = new Date(2026, 5, 15); // June 15, 2026
    expect(formatMonth(d)).toBe("June");
    expect(formatMonthYear(d)).toBe("June 2026");
  });

  it("formats date with and without year depending on current year", () => {
    const currentYear = new Date().getFullYear();
    const thisYearIso = `${currentYear}-06-15T12:00:00Z`;
    const pastYearIso = "2020-06-15T12:00:00Z";

    expect(formatDate(thisYearIso)).toMatch(/15\sJun/);
    expect(formatDate(pastYearIso)).toMatch(/15\sJun\s2020/);
    expect(formatDateWithYear(thisYearIso)).toMatch(new RegExp(`15\\sJun\\s${currentYear}`));
  });

  it("formats date ranges symmetrically", () => {
    const range = formatDateRange("2026-06-01T00:00:00Z", "2026-06-30T00:00:00Z");
    expect(range).toMatch(/1\sJun.*–.*30\sJun/);
  });
});
