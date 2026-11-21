import { describe, expect, it, vi } from "vitest";
import { RollingPeriodTable } from "./rolling-period-table";
import { renderWithProviders, screen, userEvent } from "@/tests/helpers/test-utils";
import type { PeriodPerformance } from "@/lib/api/portfolio-timeseries";

const mockPeriods: PeriodPerformance[] = [
  {
    period: "1M",
    start_date: "2026-01-01",
    end_date: "2026-01-31",
    twr_pct: 0.05,
    twr_annualized: null,
    xirr: 0.12,
    benchmark_xirr: 0.1,
    alpha: 0.02,
    max_drawdown: -0.01,
    volatility_annualized: 0.11,
    active_days: 31,
  },
  {
    period: "1Y",
    start_date: "2025-01-01",
    end_date: "2026-01-01",
    twr_pct: 0.25,
    twr_annualized: 0.25,
    xirr: 0.22,
    benchmark_xirr: 0.15,
    alpha: 0.07,
    max_drawdown: -0.06,
    volatility_annualized: 0.14,
    active_days: 365,
  },
];

describe("RollingPeriodTable", () => {
  it("renders table headers and period rows", () => {
    renderWithProviders(
      <RollingPeriodTable
        periods={mockPeriods}
        selectedPeriod="1M"
        onSelectPeriod={vi.fn()}
        benchmarkName="Nifty 50"
      />,
    );

    expect(screen.getByText("Rolling Period Breakdown")).toBeInTheDocument();
    expect(screen.getByText("TWR")).toBeInTheDocument();
    expect(screen.getByText("Annualized TWR")).toBeInTheDocument();
    expect(screen.getByText("Alpha (α)")).toBeInTheDocument();
    expect(screen.getByText("Max Drawdown")).toBeInTheDocument();

    // Check 1M row metrics
    expect(screen.getByText("+5.0%")).toBeInTheDocument();
    expect(screen.getByText("+12.0%")).toBeInTheDocument();
    expect(screen.getByText("+2.00%")).toBeInTheDocument(); // Alpha

    // Active indicator
    expect(screen.getByText("(active)")).toBeInTheDocument();
  });

  it("calls onSelectPeriod when another period row is clicked", async () => {
    const user = userEvent.setup();
    const handleSelectPeriod = vi.fn();

    renderWithProviders(
      <RollingPeriodTable
        periods={mockPeriods}
        selectedPeriod="1M"
        onSelectPeriod={handleSelectPeriod}
        benchmarkName="Nifty 50"
      />,
    );

    const yearRowLabel = screen.getByText("1Y");
    await user.click(yearRowLabel);

    expect(handleSelectPeriod).toHaveBeenCalledWith("1Y");
  });
});
