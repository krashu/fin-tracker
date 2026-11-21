import { describe, expect, it, vi } from "vitest";
import { PortfolioPerformanceChart } from "./portfolio-performance-chart";
import { renderWithProviders, screen, userEvent } from "@/tests/helpers/test-utils";
import type { PortfolioTimeseriesPoint, PeriodPerformance } from "@/lib/api/portfolio-timeseries";

const mockPoints: PortfolioTimeseriesPoint[] = [
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
    portfolio_value_paise: 10500000,
    invested_capital_paise: 9000000,
    twr_index_micros: 1050000,
    benchmark_value_paise: 10200000,
    daily_return: 0.05,
    net_cashflow_paise: 0,
    is_invested: false, // dormant liquidation gap
  },
  {
    date: "2026-02-01",
    portfolio_value_paise: 11000000,
    invested_capital_paise: 9000000,
    twr_index_micros: 1100000,
    benchmark_value_paise: 10400000,
    daily_return: 0.04,
    net_cashflow_paise: 0,
    is_invested: true,
  },
];

const mockPeriods: PeriodPerformance[] = [
  {
    period: "1M",
    start_date: "2026-01-01",
    end_date: "2026-02-01",
    twr_pct: 0.1,
    twr_annualized: null,
    xirr: 0.15,
    benchmark_xirr: 0.08,
    alpha: 0.07,
    max_drawdown: -0.02,
    volatility_annualized: 0.12,
    active_days: 31,
  },
];

describe("PortfolioPerformanceChart", () => {
  it("renders performance chart header and rolling period pills", () => {
    const handleSelectPeriod = vi.fn();
    renderWithProviders(
      <PortfolioPerformanceChart
        points={mockPoints}
        periods={mockPeriods}
        benchmarkName="Nifty 50"
        selectedPeriod="1M"
        onSelectPeriod={handleSelectPeriod}
      />,
      { initialBalanceHidden: false },
    );

    expect(screen.getByText("Performance (TWR vs. Benchmark)")).toBeInTheDocument();
    expect(screen.getByText(/Nifty 50/)).toBeInTheDocument();
    expect(screen.getByText("+10.0%")).toBeInTheDocument(); // 1M badge

    // Rolling period pills
    expect(screen.getByRole("button", { name: "1M" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "3M" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "1Y" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "All" })).toBeInTheDocument();

    // Mode toggle buttons
    expect(screen.getByRole("button", { name: "% Return" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Base 100" })).toBeInTheDocument();
  });

  it("calls onSelectPeriod when another period pill is clicked", async () => {
    const user = userEvent.setup();
    const handleSelectPeriod = vi.fn();

    renderWithProviders(
      <PortfolioPerformanceChart
        points={mockPoints}
        periods={mockPeriods}
        benchmarkName="Nifty 50"
        selectedPeriod="1M"
        onSelectPeriod={handleSelectPeriod}
      />,
    );

    const yearButton = screen.getByRole("button", { name: "1Y" });
    await user.click(yearButton);

    expect(handleSelectPeriod).toHaveBeenCalledWith("1Y");
  });

  it("renders dormant interval notice when dormant gap exists", () => {
    renderWithProviders(
      <PortfolioPerformanceChart
        points={mockPoints}
        periods={mockPeriods}
        benchmarkName="Nifty 50"
        selectedPeriod="all"
        onSelectPeriod={vi.fn()}
      />,
    );

    expect(
      screen.getByText(/Shaded zones indicate dormant periods/),
    ).toBeInTheDocument();
  });

  it("handles empty points state cleanly", () => {
    renderWithProviders(
      <PortfolioPerformanceChart
        points={[]}
        periods={[]}
        benchmarkName="Nifty 50"
        selectedPeriod="all"
        onSelectPeriod={vi.fn()}
      />,
    );

    expect(
      screen.getByText("No timeseries points recorded for this period."),
    ).toBeInTheDocument();
  });
});
