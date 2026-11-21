/**
 * Portfolio timeseries & valuation history API client (Milestone 2 Phase 5).
 *
 * Calls `GET /api/v1/portfolio/timeseries` to fetch daily forward-sweep valuation,
 * unitized TWR index, counterfactual benchmark value, and period performance metrics.
 */

import { useQuery } from "@tanstack/react-query";
import { request } from "@/lib/api/client";

export type ScopeType = "all" | "account" | "member";

export type PortfolioTimeseriesPoint = {
  date: string; // "YYYY-MM-DD"
  portfolio_value_paise: number; // int64
  invested_capital_paise: number; // int64
  twr_index_micros: number; // int64 (10^-4 scale, base 100.0 * 10^4 = 1,000,000)
  benchmark_value_paise: number | null; // counterfactual mPME in paise
  daily_return: number; // float
  net_cashflow_paise: number; // int64
  is_invested: boolean; // false during liquidation gaps or dormant periods
};

export type PeriodPerformance = {
  period: string; // "1M" | "3M" | "6M" | "1Y" | "3Y" | "5Y" | "all"
  start_date: string;
  end_date: string;
  twr_pct: number | null;
  twr_annualized: number | null;
  xirr: number | null;
  benchmark_xirr: number | null;
  alpha: number | null;
  max_drawdown: number;
  volatility_annualized: number | null;
  active_days: number;
};

export type PortfolioTimeseriesResponse = {
  scope: ScopeType;
  account_id: number | null;
  start_date: string;
  end_date: string;
  benchmark_id: number | null;
  benchmark_name: string | null;
  points: PortfolioTimeseriesPoint[];
  periods: PeriodPerformance[];
};

export type FetchPortfolioTimeseriesParams = {
  scope?: ScopeType;
  account_id?: number | null;
  from_date?: string;
  to_date?: string;
  benchmark_id?: number | null;
  persist?: boolean;
};

export function fetchPortfolioTimeseries(
  params?: FetchPortfolioTimeseriesParams,
): Promise<PortfolioTimeseriesResponse> {
  const qs = new URLSearchParams();
  if (params?.scope) {
    qs.set("scope", params.scope);
  }
  if (params?.account_id != null) {
    qs.set("account_id", String(params.account_id));
  }
  if (params?.from_date) {
    qs.set("from_date", params.from_date);
  }
  if (params?.to_date) {
    qs.set("to_date", params.to_date);
  }
  if (params?.benchmark_id != null) {
    qs.set("benchmark_id", String(params.benchmark_id));
  }
  if (params?.persist) {
    qs.set("persist", "true");
  }
  const s = qs.toString();
  return request<PortfolioTimeseriesResponse>(
    `/portfolio/timeseries${s ? `?${s}` : ""}`,
  );
}

export function usePortfolioTimeseries(
  params?: FetchPortfolioTimeseriesParams,
  options?: { enabled?: boolean },
) {
  return useQuery({
    queryKey: ["portfolio", "timeseries", params],
    queryFn: () => fetchPortfolioTimeseries(params),
    enabled: options?.enabled ?? true,
  });
}
