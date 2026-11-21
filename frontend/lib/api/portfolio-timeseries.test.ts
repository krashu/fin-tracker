import { describe, expect, it, vi, beforeEach } from "vitest";
import {
  fetchPortfolioTimeseries,
  type PortfolioTimeseriesResponse,
} from "./portfolio-timeseries";
import * as client from "@/lib/api/client";

vi.mock("@/lib/api/client", () => ({
  request: vi.fn(),
}));

describe("portfolio-timeseries API", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  const mockResponse: PortfolioTimeseriesResponse = {
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
    ],
    periods: [
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
        volatility_annualized: 0.15,
        active_days: 31,
      },
    ],
  };

  it("fetches timeseries without parameters", async () => {
    vi.mocked(client.request).mockResolvedValueOnce(mockResponse);

    const data = await fetchPortfolioTimeseries();
    expect(client.request).toHaveBeenCalledWith("/portfolio/timeseries");
    expect(data.points).toHaveLength(1);
    expect(data.benchmark_name).toBe("Nifty 50");
  });

  it("fetches timeseries with query parameters", async () => {
    vi.mocked(client.request).mockResolvedValueOnce(mockResponse);

    await fetchPortfolioTimeseries({
      scope: "account",
      account_id: 42,
      from_date: "2026-01-01",
      to_date: "2026-01-31",
      benchmark_id: 2,
      persist: true,
    });

    expect(client.request).toHaveBeenCalledWith(
      "/portfolio/timeseries?scope=account&account_id=42&from_date=2026-01-01&to_date=2026-01-31&benchmark_id=2&persist=true",
    );
  });
});
