import { describe, expect, it } from "vitest";
import { PortfolioStatTiles } from "./portfolio-stat-tiles";
import { renderWithProviders, screen } from "@/tests/helpers/test-utils";
import type { PortfolioTimeseriesPoint, PeriodPerformance } from "@/lib/api/portfolio-timeseries";

const mockPoints: PortfolioTimeseriesPoint[] = [
  {
    date: "2025-01-01",
    portfolio_value_paise: 10000000,
    invested_capital_paise: 9000000,
    twr_index_micros: 1000000,
    benchmark_value_paise: 10000000,
    daily_return: 0.0,
    net_cashflow_paise: 9000000,
    is_invested: true,
  },
  {
    date: "2026-01-01",
    portfolio_value_paise: 12000000, // ₹1,20,000.00
    invested_capital_paise: 10000000, // ₹1,00,000.00 (Gain: +₹20,000.00, +20.0%)
    twr_index_micros: 1200000,
    benchmark_value_paise: 11000000,
    daily_return: 0.02,
    net_cashflow_paise: 1000000,
    is_invested: true,
  },
];

const mockPeriods: PeriodPerformance[] = [
  {
    period: "1Y",
    start_date: "2025-01-01",
    end_date: "2026-01-01",
    twr_pct: 0.2, // +20.0%
    twr_annualized: 0.2, // +20.0% p.a.
    xirr: 0.22, // +22.0%
    benchmark_xirr: 0.15, // +15.0%
    alpha: 0.07, // +7.00%
    max_drawdown: -0.05, // -5.00%
    volatility_annualized: 0.14, // 14.0%
    active_days: 365,
  },
];

describe("PortfolioStatTiles", () => {
  it("renders key performance indicator tiles accurately", () => {
    renderWithProviders(
      <PortfolioStatTiles
        points={mockPoints}
        periods={mockPeriods}
        selectedPeriod="1Y"
        benchmarkName="Nifty 50"
      />,
      { initialBalanceHidden: false },
    );

    // 1. Current Value & Gain
    expect(screen.getByText("Current Value")).toBeInTheDocument();
    expect(screen.getByText("₹1,20,000.00")).toBeInTheDocument();
    expect(screen.getByText("+₹20,000.00")).toBeInTheDocument();
    expect(screen.getByText("(+20.0%)")).toBeInTheDocument();

    // 2. Period TWR
    expect(screen.getByText("TWR (1Y)")).toBeInTheDocument();
    expect(screen.getByText("+20.0%")).toBeInTheDocument();
    expect(screen.getByText("+20.0% p.a.")).toBeInTheDocument(); // CAGR

    // 3. XIRR & Benchmark Alpha
    expect(screen.getByText("XIRR vs. Benchmark")).toBeInTheDocument();
    expect(screen.getByText("+22.0%")).toBeInTheDocument();
    expect(screen.getByText("+15.0%")).toBeInTheDocument(); // Benchmark XIRR
    expect(screen.getByText("+7.00%")).toBeInTheDocument(); // Alpha

    // 4. Max Drawdown & Volatility
    expect(screen.getByText("Max Drawdown")).toBeInTheDocument();
    expect(screen.getByText("-5.00%")).toBeInTheDocument();
    expect(screen.getByText("Ann. Volatility: 14.0%")).toBeInTheDocument();
  });

  it("masks monetary figures under balance hidden mode", () => {
    renderWithProviders(
      <PortfolioStatTiles
        points={mockPoints}
        periods={mockPeriods}
        selectedPeriod="1Y"
        benchmarkName="Nifty 50"
      />,
      { initialBalanceHidden: true },
    );

    expect(screen.queryByText("₹1,20,000.00")).not.toBeInTheDocument();
    expect(screen.queryByText("+₹20,000.00")).not.toBeInTheDocument();
    // Non-money ratios like TWR and XIRR remain visible
    expect(screen.getByText("+20.0%")).toBeInTheDocument();
  });

  it("handles loading or empty state cleanly with dashes", () => {
    renderWithProviders(
      <PortfolioStatTiles
        points={[]}
        periods={[]}
        selectedPeriod="1Y"
        isLoading={true}
      />,
    );

    const dashes = screen.getAllByText("—");
    expect(dashes.length).toBeGreaterThanOrEqual(4);
  });
});
