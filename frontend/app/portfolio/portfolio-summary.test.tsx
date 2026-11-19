import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { PortfolioSummary } from "./portfolio-summary";
import { renderWithProviders, screen } from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";
import type { PortfolioSummaryResponse } from "@/lib/api/client";

const matureSummary: PortfolioSummaryResponse = {
  current_value_paise: 10350000,
  invested_paise: 8970000,
  unrealized_pnl_paise: 1380000,
  price_pnl_paise: 1110000,
  fx_pnl_paise: 270000,
  xirr: 0.182,
  rate: 0.182,
  return_type: "xirr",
  hpr: 0.182,
  duration_days: 420,
  holdings_count: 2,
  null_nav_count: 0,
  fx_unavailable_count: 0,
  allocations: [
    { asset_class: "indian_equity", value_paise: 5000000 },
    { asset_class: "us_equity", value_paise: 5350000 },
  ],
  holding_xirr: [],
};

const shortHoldingSummary: PortfolioSummaryResponse = {
  current_value_paise: 5200000,
  invested_paise: 5000000,
  unrealized_pnl_paise: 200000,
  price_pnl_paise: 200000,
  fx_pnl_paise: 0,
  xirr: null,
  rate: 0.045,
  return_type: "hpr",
  hpr: 0.045,
  duration_days: 85,
  holdings_count: 1,
  null_nav_count: 0,
  fx_unavailable_count: 0,
  allocations: [{ asset_class: "indian_equity", value_paise: 5200000 }],
  holding_xirr: [],
};

describe("PortfolioSummary", () => {
  it("renders XIRR eyebrow and annualized subtitle for mature portfolio", async () => {
    server.use(
      http.get("*/api/v1/portfolio/summary", () =>
        HttpResponse.json(matureSummary),
      ),
    );

    renderWithProviders(<PortfolioSummary />);

    expect(await screen.findByText("+18.2%")).toBeInTheDocument();
    expect(screen.getByText("XIRR")).toBeInTheDocument();
    expect(screen.getByText("annualized on current holdings")).toBeInTheDocument();
    expect(screen.getByText("Prices up to date")).toBeInTheDocument();
  });

  it("renders RETURN (HPR) eyebrow, duration subtitle and Holding < 1y badge for short portfolio", async () => {
    server.use(
      http.get("*/api/v1/portfolio/summary", () =>
        HttpResponse.json(shortHoldingSummary),
      ),
    );

    renderWithProviders(<PortfolioSummary />);

    expect(await screen.findByText("+4.5%")).toBeInTheDocument();
    expect(screen.getByText("RETURN (HPR)")).toBeInTheDocument();
    expect(screen.getByText("Holding < 1y")).toBeInTheDocument();
    expect(screen.getByText("absolute return over 85d")).toBeInTheDocument();
  });

  it("renders Asset and FX breakdown under Unrealized P&L when foreign holdings present", async () => {
    server.use(
      http.get("*/api/v1/portfolio/summary", () =>
        HttpResponse.json(matureSummary),
      ),
    );

    renderWithProviders(<PortfolioSummary />);

    expect(await screen.findByText("Asset:")).toBeInTheDocument();
    expect(screen.getByText("FX:")).toBeInTheDocument();
  });

  it("hides Asset and FX breakdown under Unrealized P&L for domestic-only portfolio", async () => {
    server.use(
      http.get("*/api/v1/portfolio/summary", () =>
        HttpResponse.json(shortHoldingSummary),
      ),
    );

    renderWithProviders(<PortfolioSummary />);

    // Wait for data to load
    expect(await screen.findByText("RETURN (HPR)")).toBeInTheDocument();
    // Asset: and FX: should not appear
    expect(screen.queryByText("Asset:")).not.toBeInTheDocument();
    expect(screen.queryByText("FX:")).not.toBeInTheDocument();
  });
});
