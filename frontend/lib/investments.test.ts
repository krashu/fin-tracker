import { describe, expect, it } from "vitest";
import type { InstrumentRead } from "@/lib/api/client";
import {
  ASSET_CLASS_LABELS,
  ENTRY_MODE_LABELS,
  ENTRY_MODES,
  EXCHANGE_LABELS,
  INVESTMENT_TYPE_LABELS,
  MANUAL_ENTRY_TYPES,
  STALENESS_WARN_DAYS,
  filterPointsByPeriod,
  instrumentLabel,
  rebaseTimeseriesPoints,
} from "@/lib/investments";

describe("investments metadata & constants", () => {
  it("defines staleness threshold matching backend (4 calendar days)", () => {
    expect(STALENESS_WARN_DAYS).toBe(4);
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

  it("rebases timeseries points to percentage return correctly", () => {
    const points = [
      {
        date: "2026-01-01",
        portfolio_value_paise: 10000000,
        invested_capital_paise: 9000000,
        twr_index_micros: 1000000, // base 100
        benchmark_value_paise: 10000000,
        daily_return: 0.0,
        net_cashflow_paise: 0,
        is_invested: true,
      },
      {
        date: "2026-01-02",
        portfolio_value_paise: 10500000,
        invested_capital_paise: 9000000,
        twr_index_micros: 1050000, // +5%
        benchmark_value_paise: 10200000, // +2%
        daily_return: 0.05,
        net_cashflow_paise: 0,
        is_invested: true,
      },
    ];

    const rebased = rebaseTimeseriesPoints(points, "pct");
    expect(rebased).toHaveLength(2);
    expect(rebased[0].portfolioReturn).toBe(0);
    expect(rebased[0].benchmarkReturn).toBe(0);
    expect(rebased[1].portfolioReturn).toBe(5);
    expect(rebased[1].benchmarkReturn).toBe(2);
  });

  it("rebases timeseries points to base 100 index correctly", () => {
    const points = [
      {
        date: "2026-01-01",
        portfolio_value_paise: 10000000,
        invested_capital_paise: 9000000,
        twr_index_micros: 1000000,
        benchmark_value_paise: 10000000,
        daily_return: 0.0,
        net_cashflow_paise: 0,
        is_invested: true,
      },
      {
        date: "2026-01-02",
        portfolio_value_paise: 11000000,
        invested_capital_paise: 9000000,
        twr_index_micros: 1100000,
        benchmark_value_paise: 10500000,
        daily_return: 0.1,
        net_cashflow_paise: 0,
        is_invested: true,
      },
    ];

    const rebased = rebaseTimeseriesPoints(points, "base100");
    expect(rebased[0].portfolioReturn).toBe(100);
    expect(rebased[0].benchmarkReturn).toBe(100);
    expect(rebased[1].portfolioReturn).toBe(110);
    expect(rebased[1].benchmarkReturn).toBe(105);
  });

  it("filters points by period matching period start_date", () => {
    const points = [
      {
        date: "2026-01-01",
        portfolio_value_paise: 10000000,
        invested_capital_paise: 9000000,
        twr_index_micros: 1000000,
        benchmark_value_paise: 10000000,
        daily_return: 0.0,
        net_cashflow_paise: 0,
        is_invested: true,
      },
      {
        date: "2026-01-15",
        portfolio_value_paise: 10200000,
        invested_capital_paise: 9000000,
        twr_index_micros: 1020000,
        benchmark_value_paise: 10100000,
        daily_return: 0.02,
        net_cashflow_paise: 0,
        is_invested: true,
      },
      {
        date: "2026-02-01",
        portfolio_value_paise: 10500000,
        invested_capital_paise: 9000000,
        twr_index_micros: 1050000,
        benchmark_value_paise: 10300000,
        daily_return: 0.03,
        net_cashflow_paise: 0,
        is_invested: true,
      },
    ];

    const periods = [
      {
        period: "1M",
        start_date: "2026-01-15",
        end_date: "2026-02-01",
        twr_pct: 0.03,
        twr_annualized: null,
        xirr: 0.1,
        benchmark_xirr: 0.08,
        alpha: 0.02,
        max_drawdown: 0,
        volatility_annualized: 0.1,
        active_days: 17,
      },
    ];

    const filtered = filterPointsByPeriod(points, "1M", periods);
    expect(filtered).toHaveLength(2);
    expect(filtered[0].date).toBe("2026-01-15");
    expect(filtered[1].date).toBe("2026-02-01");

    const allFiltered = filterPointsByPeriod(points, "all", periods);
    expect(allFiltered).toHaveLength(3);
  });
});
