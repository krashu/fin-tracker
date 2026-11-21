import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { PortfolioDashboardView } from "./portfolio-dashboard-view";
import { renderWithProviders, screen } from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";
import type { PortfolioTimeseriesResponse } from "@/lib/api/portfolio-timeseries";
import type { InvestmentAccountRead } from "@/lib/types/investment-accounts";
import type { Benchmark } from "@/lib/api/client";

const mockAccounts: InvestmentAccountRead[] = [
  {
    id: 1,
    name: "Zerodha Demat",
    account_type: "demat",
    provider: "zerodha",
    external_account_ref: "12345",
    currency: "INR",
    is_active: true,
  },
];

const mockBenchmarks: Benchmark[] = [
  {
    id: 1,
    name: "Nifty 50",
    kind: "index_fund",
    amfi_code: "120716",
    currency: "INR",
    inception_date: "2013-01-01",
  },
];

const mockTimeseries: PortfolioTimeseriesResponse = {
  scope: "all",
  account_id: null,
  start_date: "2026-01-01",
  end_date: "2026-01-31",
  benchmark_id: 1,
  benchmark_name: "Nifty 50",
  points: [
    {
      date: "2026-01-01",
      portfolio_value_paise: 10000000,
      invested_capital_paise: 9000000,
      twr_index_micros: 1000000,
      benchmark_value_paise: 9000000,
      daily_return: 0.0,
      net_cashflow_paise: 9000000,
      is_invested: true,
    },
    {
      date: "2026-01-31",
      portfolio_value_paise: 11000000,
      invested_capital_paise: 9000000,
      twr_index_micros: 1100000,
      benchmark_value_paise: 9500000,
      daily_return: 0.1,
      net_cashflow_paise: 0,
      is_invested: true,
    },
  ],
  periods: [
    {
      period: "1M",
      start_date: "2026-01-01",
      end_date: "2026-01-31",
      twr_pct: 0.1,
      twr_annualized: null,
      xirr: 0.15,
      benchmark_xirr: 0.08,
      alpha: 0.07,
      max_drawdown: -0.01,
      volatility_annualized: 0.12,
      active_days: 31,
    },
  ],
};

describe("PortfolioDashboardView", () => {
  it("renders live dashboard when data loads successfully", async () => {
    server.use(
      http.get("*/api/v1/investment-accounts", () =>
        HttpResponse.json(mockAccounts),
      ),
      http.get("*/api/v1/benchmarks", () =>
        HttpResponse.json(mockBenchmarks),
      ),
      http.get("*/api/v1/portfolio/timeseries", () =>
        HttpResponse.json(mockTimeseries),
      ),
    );

    renderWithProviders(<PortfolioDashboardView />);

    // Wait for the components to render data
    expect(await screen.findByText("Performance (TWR vs. Benchmark)")).toBeInTheDocument();
    expect(screen.getByText("Valuation & Capital Curve")).toBeInTheDocument();
    expect(screen.getByText("Rolling Period Breakdown")).toBeInTheDocument();
    expect(screen.getByText("Current Value")).toBeInTheDocument();
  });

  it("renders empty state when there are zero timeseries points", async () => {
    const emptyTimeseries: PortfolioTimeseriesResponse = {
      ...mockTimeseries,
      points: [],
      periods: [],
    };

    server.use(
      http.get("*/api/v1/investment-accounts", () =>
        HttpResponse.json(mockAccounts),
      ),
      http.get("*/api/v1/benchmarks", () =>
        HttpResponse.json(mockBenchmarks),
      ),
      http.get("*/api/v1/portfolio/timeseries", () =>
        HttpResponse.json(emptyTimeseries),
      ),
    );

    renderWithProviders(<PortfolioDashboardView />);

    expect(
      await screen.findByText("No investment history recorded"),
    ).toBeInTheDocument();
    expect(screen.getByText("Record Transaction")).toBeInTheDocument();
    expect(screen.getByText("Import Statements")).toBeInTheDocument();
  });

  it("renders error state when timeseries API fails", async () => {
    server.use(
      http.get("*/api/v1/investment-accounts", () =>
        HttpResponse.json(mockAccounts),
      ),
      http.get("*/api/v1/benchmarks", () =>
        HttpResponse.json(mockBenchmarks),
      ),
      http.get("*/api/v1/portfolio/timeseries", () =>
        new HttpResponse(null, { status: 500 }),
      ),
    );

    renderWithProviders(<PortfolioDashboardView />);

    expect(
      await screen.findByText(/Couldn’t load portfolio timeseries data/),
    ).toBeInTheDocument();
  });
});
