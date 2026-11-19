import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { HoldingsTable } from "./holdings-table";
import { renderWithProviders, screen } from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";
import type { HoldingRead, PortfolioSummaryResponse } from "@/lib/api/client";

const mockHoldings: HoldingRead[] = [
  {
    instrument_id: 1,
    symbol: "INFY",
    name: "Infosys Ltd",
    asset_class: "indian_equity",
    currency: "INR",
    net_units: "10",
    avg_cost_native: "1500.00",
    invested_native_paise: 1500000,
    current_nav: "1650.00",
    current_value_native_paise: 1650000,
    unrealized_pnl_native_paise: 150000,
    invested_inr_paise: 1500000,
    current_value_inr_paise: 1650000,
    unrealized_pnl_inr_paise: 150000,
    price_pnl_inr_paise: 150000,
    fx_pnl_inr_paise: 0,
    nav_staleness_days: 0,
    supports_price_feed: true,
  },
  {
    instrument_id: 2,
    symbol: "AAPL",
    name: "Apple Inc",
    asset_class: "us_equity",
    currency: "USD",
    net_units: "5",
    avg_cost_native: "180.00",
    invested_native_paise: 90000,
    current_nav: "200.00",
    current_value_native_paise: 100000,
    unrealized_pnl_native_paise: 10000,
    invested_inr_paise: 7470000,
    current_value_inr_paise: 8600000,
    unrealized_pnl_inr_paise: 1130000,
    price_pnl_inr_paise: 860000,
    fx_pnl_inr_paise: 270000,
    nav_staleness_days: 1,
    supports_price_feed: true,
  },
  {
    instrument_id: 3,
    symbol: "BONUS_SHARE",
    name: "Bonus Shares Ltd",
    asset_class: "indian_equity",
    currency: "INR",
    net_units: "20",
    avg_cost_native: "0.00",
    invested_native_paise: 0,
    current_nav: "50.00",
    current_value_native_paise: 100000,
    unrealized_pnl_native_paise: 100000,
    invested_inr_paise: 0,
    current_value_inr_paise: 100000,
    unrealized_pnl_inr_paise: 100000,
    price_pnl_inr_paise: 100000,
    fx_pnl_inr_paise: 0,
    nav_staleness_days: 0,
    supports_price_feed: true,
  },
];

const mockSummary: PortfolioSummaryResponse = {
  current_value_paise: 10350000,
  invested_paise: 8970000,
  unrealized_pnl_paise: 1380000,
  price_pnl_paise: 1110000,
  fx_pnl_paise: 270000,
  xirr: 0.154,
  rate: 0.154,
  return_type: "xirr",
  hpr: 0.154,
  duration_days: 420,
  holdings_count: 3,
  null_nav_count: 0,
  fx_unavailable_count: 0,
  allocations: [],
  holding_xirr: [
    {
      instrument_id: 1,
      xirr: 0.184,
      rate: 0.184,
      return_type: "xirr",
      hpr: 0.1,
      duration_days: 500,
    },
    {
      instrument_id: 2,
      xirr: null,
      rate: 0.111,
      return_type: "hpr",
      hpr: 0.111,
      duration_days: 60,
    },
    {
      instrument_id: 3,
      xirr: null,
      rate: null,
      return_type: null,
      hpr: null,
      duration_days: null,
    },
  ],
};

describe("HoldingsTable", () => {
  it("renders Return header with explanatory tooltip", async () => {
    server.use(
      http.get("*/api/v1/holdings", () =>
        HttpResponse.json({ holdings: mockHoldings }),
      ),
      http.get("*/api/v1/portfolio/summary", () =>
        HttpResponse.json(mockSummary),
      ),
    );

    renderWithProviders(<HoldingsTable />, { initialBalanceHidden: false });

    const returnHeader = await screen.findByText("Return");
    expect(returnHeader).toBeInTheDocument();
    expect(returnHeader).toHaveAttribute(
      "title",
      "Annualized XIRR for investments held ≥ 1 year; Absolute Return (HPR) for investments held < 1 year.",
    );
  });

  it("renders mature position with XIRR and short-duration with HPR badge", async () => {
    server.use(
      http.get("*/api/v1/holdings", () =>
        HttpResponse.json({ holdings: mockHoldings }),
      ),
      http.get("*/api/v1/portfolio/summary", () =>
        HttpResponse.json(mockSummary),
      ),
    );

    renderWithProviders(<HoldingsTable />, { initialBalanceHidden: false });

    // INFY is mature (duration 500d) -> renders +18.4%
    expect(await screen.findByText("+18.4%")).toBeInTheDocument();

    // AAPL is short-duration (60d) -> renders +11.1% and <HPR> badge
    expect(await screen.findByText("+11.1%")).toBeInTheDocument();
    const hprBadge = await screen.findByText("HPR");
    expect(hprBadge).toBeInTheDocument();
    expect(hprBadge).toHaveAttribute(
      "title",
      "Held for 60 days (< 1 year). Absolute return (HPR).",
    );
  });

  it("renders dual price and FX breakdown tooltip for USD holdings and hides for INR holdings", async () => {
    server.use(
      http.get("*/api/v1/holdings", () =>
        HttpResponse.json({ holdings: mockHoldings }),
      ),
      http.get("*/api/v1/portfolio/summary", () =>
        HttpResponse.json(mockSummary),
      ),
    );

    renderWithProviders(<HoldingsTable />, { initialBalanceHidden: false });

    // USD holding (AAPL) shows total INR P&L with Price & FX breakdown in title tooltip
    const breakdownEl = await screen.findByTitle(/Price:.*FX:/);
    expect(breakdownEl).toBeInTheDocument();
    expect(breakdownEl).toHaveAttribute(
      "title",
      expect.stringContaining("Price:"),
    );
    expect(breakdownEl).toHaveAttribute(
      "title",
      expect.stringContaining("FX:"),
    );
    expect(breakdownEl).toHaveTextContent(/11,300\.00/);

    // Only one Price: / FX: breakdown tooltip appears (from AAPL, none for INFY or BONUS_SHARE)
    expect(screen.getAllByTitle(/Price:.*FX:/)).toHaveLength(1);
  });

  it("renders dash with zero-cost basis tooltip for bonus share holdings", async () => {
    server.use(
      http.get("*/api/v1/holdings", () =>
        HttpResponse.json({ holdings: mockHoldings }),
      ),
      http.get("*/api/v1/portfolio/summary", () =>
        HttpResponse.json(mockSummary),
      ),
    );

    renderWithProviders(<HoldingsTable />, { initialBalanceHidden: false });

    const bonusDash = await screen.findByTitle(
      "100% bonus units / zero cost basis",
    );
    expect(bonusDash).toBeInTheDocument();
    expect(bonusDash).toHaveTextContent("—");
  });
});
