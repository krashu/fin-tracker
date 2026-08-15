import { describe, expect, it } from "vitest";
import type { InstrumentRead } from "@/lib/api/client";
import {
  ASSET_CLASS_LABELS,
  ENTRY_MODE_LABELS,
  ENTRY_MODES,
  EXCHANGE_LABELS,
  INVESTMENT_TYPE_LABELS,
  MANUAL_ENTRY_TYPES,
  MANUALLY_PRICED_CLASSES,
  STALENESS_WARN_DAYS,
  instrumentLabel,
} from "@/lib/investments";

describe("investments metadata & constants", () => {
  it("defines staleness threshold matching backend (4 calendar days)", () => {
    expect(STALENESS_WARN_DAYS).toBe(4);
  });

  it("identifies manually priced asset classes without auto price feeds", () => {
    expect(MANUALLY_PRICED_CLASSES.has("fd")).toBe(true);
    expect(MANUALLY_PRICED_CLASSES.has("bond")).toBe(true);
    expect(MANUALLY_PRICED_CLASSES.has("nps")).toBe(true);
    expect(MANUALLY_PRICED_CLASSES.has("gold")).toBe(true);
    expect(MANUALLY_PRICED_CLASSES.has("other")).toBe(true);

    // Auto-priced classes should not be manual
    expect(MANUALLY_PRICED_CLASSES.has("indian_equity")).toBe(false);
    expect(MANUALLY_PRICED_CLASSES.has("indian_mf")).toBe(false);
    expect(MANUALLY_PRICED_CLASSES.has("us_equity")).toBe(false);
    expect(MANUALLY_PRICED_CLASSES.has("us_etf")).toBe(false);
  });

  it("formats instrument labels consistently", () => {
    const inst: InstrumentRead = {
      id: 1,
      symbol: "HDFCBANK",
      name: "HDFC Bank Ltd",
      asset_class: "indian_equity",
      exchange: "NSE",
      currency: "INR",
      isin: null,
      current_nav: null,
      nav_updated_at: null,
      archived_at: null,
    };
    expect(instrumentLabel(inst)).toBe("HDFCBANK — HDFC Bank Ltd");
  });

  it("distinguishes manual entry types from system/CAS types", () => {
    expect(MANUAL_ENTRY_TYPES).toEqual([
      "buy",
      "sip",
      "sell",
      "dividend",
      "bonus",
    ]);

    // Split and switch_in/switch_out are CAS-era only
    expect(MANUAL_ENTRY_TYPES).not.toContain("split");
    expect(MANUAL_ENTRY_TYPES).not.toContain("switch_in");
    expect(MANUAL_ENTRY_TYPES).not.toContain("switch_out");
  });

  it("includes reinvestment in entry modes with specialized label", () => {
    expect(ENTRY_MODES).toContain("reinvestment");
    expect(ENTRY_MODE_LABELS.reinvestment).toBe("IDCW reinvestment");
  });

  it("has complete mapping for exchanges and asset classes", () => {
    expect(EXCHANGE_LABELS.NSE).toBe("NSE");
    expect(EXCHANGE_LABELS.MFCentral).toBe("MF Central");
    expect(ASSET_CLASS_LABELS.indian_mf).toBe("Indian MF");
    expect(INVESTMENT_TYPE_LABELS.dividend).toBe("Dividend");
  });
});
