import { describe, expect, it } from "vitest";
import { ValuationCapitalChart } from "./valuation-capital-chart";
import { renderWithProviders, screen, userEvent } from "@/tests/helpers/test-utils";
import type { PortfolioTimeseriesPoint } from "@/lib/api/portfolio-timeseries";

const mockPoints: PortfolioTimeseriesPoint[] = [
  {
    date: "2026-01-01",
    portfolio_value_paise: 10000000, // ₹1,00,000.00
    invested_capital_paise: 9000000, // ₹90,000.00
    twr_index_micros: 1000000,
    benchmark_value_paise: 9500000, // ₹95,000.00
    daily_return: 0.0,
    net_cashflow_paise: 9000000,
    is_invested: true,
  },
  {
    date: "2026-01-15",
    portfolio_value_paise: 11000000, // ₹1,10,000.00
    invested_capital_paise: 9000000, // ₹90,000.00
    twr_index_micros: 1100000,
    benchmark_value_paise: 10200000, // ₹1,02,000.00
    daily_return: 0.1,
    net_cashflow_paise: 0,
    is_invested: true,
  },
];

describe("ValuationCapitalChart", () => {
  it("renders valuation chart with latest portfolio value and legend", () => {
    renderWithProviders(
      <ValuationCapitalChart
        points={mockPoints}
        benchmarkName="Nifty 50"
        selectedPeriod="all"
      />,
      { initialBalanceHidden: false },
    );

    expect(screen.getByText("Valuation & Capital Curve")).toBeInTheDocument();
    expect(screen.getByText("₹1,10,000.00")).toBeInTheDocument();
    expect(screen.getByText("Portfolio Value")).toBeInTheDocument();
    expect(
      screen.getByText(/Net Invested Capital \(cash in\/out\)/),
    ).toBeInTheDocument();
  });

  it("masks balance when initialBalanceHidden is true", () => {
    renderWithProviders(
      <ValuationCapitalChart
        points={mockPoints}
        benchmarkName="Nifty 50"
        selectedPeriod="all"
      />,
      { initialBalanceHidden: true },
    );

    expect(screen.queryByText("₹1,10,000.00")).not.toBeInTheDocument();
    expect(screen.getByText("••••")).toBeInTheDocument();
  });

  it("allows toggling the benchmark mPME curve", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <ValuationCapitalChart
        points={mockPoints}
        benchmarkName="Nifty 50"
        selectedPeriod="all"
      />,
    );

    const toggleBtn = screen.getByRole("button", {
      name: /mPME \(Nifty 50\)/,
    });
    expect(toggleBtn).toHaveAttribute("aria-pressed", "true");

    await user.click(toggleBtn);
    expect(toggleBtn).toHaveAttribute("aria-pressed", "false");
  });

  it("handles empty points state cleanly", () => {
    renderWithProviders(
      <ValuationCapitalChart
        points={[]}
        benchmarkName="Nifty 50"
        selectedPeriod="all"
      />,
    );

    expect(
      screen.getByText("No valuation points recorded for this period."),
    ).toBeInTheDocument();
  });
});
