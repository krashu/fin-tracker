import { describe, expect, it } from "vitest";
import { HoldingsSummaryCards } from "./holdings-summary-cards";
import { renderWithProviders, screen } from "@/tests/helpers/test-utils";
import type { HoldingRead } from "@/lib/api/client";
import type { InvestmentAccountRead } from "@/lib/types/investment-accounts";

const mockDomesticHoldings: HoldingRead[] = [
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
];

const mockUsHoldings: HoldingRead[] = [
  {
    instrument_id: 2,
    symbol: "AAPL",
    name: "Apple Inc",
    asset_class: "us_equity",
    currency: "USD",
    net_units: "5",
    avg_cost_native: "180.00",
    invested_native_paise: 90000, // $900.00 in cents
    current_nav: "200.00",
    current_value_native_paise: 100000, // $1000.00 in cents
    unrealized_pnl_native_paise: 10000, // $100.00 in cents
    invested_inr_paise: 7470000, // ₹74,700.00
    current_value_inr_paise: 8600000, // ₹86,000.00
    unrealized_pnl_inr_paise: 1130000, // ₹11,300.00
    price_pnl_inr_paise: 860000,
    fx_pnl_inr_paise: 270000,
    nav_staleness_days: 1,
    supports_price_feed: true,
  },
];

const mockUsAccount: InvestmentAccountRead = {
  id: 2,
  name: "INDmoney US",
  account_type: "us_broker",
  provider: "indmoney",
  external_account_ref: "67890",
  currency: "USD",
  is_active: true,
};

describe("HoldingsSummaryCards", () => {
  it("renders domestic INR summary cards correctly", () => {
    renderWithProviders(
      <HoldingsSummaryCards
        holdings={mockDomesticHoldings}
        selectedAccount={null}
      />,
      { initialBalanceHidden: false },
    );

    expect(screen.getByText("Current Value")).toBeInTheDocument();
    expect(screen.getByText("₹16,500.00")).toBeInTheDocument();

    expect(screen.getByText("Total Invested")).toBeInTheDocument();
    expect(screen.getByText("₹15,000.00")).toBeInTheDocument();

    expect(screen.getByText("Total Gain/Loss")).toBeInTheDocument();
    expect(screen.getByText("+₹1,500.00")).toBeInTheDocument();
    expect(screen.getByText("(+10.0%)")).toBeInTheDocument();
  });

  it("renders dual-currency USD and INR summary for US broker account", () => {
    renderWithProviders(
      <HoldingsSummaryCards
        holdings={mockUsHoldings}
        selectedAccount={mockUsAccount}
      />,
      { initialBalanceHidden: false },
    );

    // Native USD values
    expect(screen.getByText("$1,000.00")).toBeInTheDocument();
    expect(screen.getByText("$900.00")).toBeInTheDocument();
    expect(screen.getByText("+$100.00")).toBeInTheDocument();

    // INR equivalents
    expect(screen.getByText("≈ ₹86,000.00")).toBeInTheDocument();
    expect(screen.getByText("≈ ₹74,700.00")).toBeInTheDocument();
    expect(screen.getByText("≈ +₹11,300.00")).toBeInTheDocument();
  });

  it("handles loading state cleanly", () => {
    renderWithProviders(
      <HoldingsSummaryCards
        holdings={[]}
        selectedAccount={null}
        isLoading={true}
      />,
      { initialBalanceHidden: false },
    );

    const dashes = screen.getAllByText("—");
    expect(dashes.length).toBeGreaterThanOrEqual(3);
  });

  it("suppresses misleading -100% loss when all holdings are unpriced (null-NAV)", () => {
    const unpricedHolding: HoldingRead = {
      ...mockDomesticHoldings[0],
      current_nav: null,
      current_value_native_paise: null,
      current_value_inr_paise: null,
      unrealized_pnl_native_paise: null,
      unrealized_pnl_inr_paise: null,
    };

    renderWithProviders(
      <HoldingsSummaryCards
        holdings={[unpricedHolding]}
        selectedAccount={null}
      />,
      { initialBalanceHidden: false },
    );

    expect(screen.getByText("1 unpriced (₹0)")).toBeInTheDocument();
    // Gain/loss renders dash "—" instead of misleading total loss
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.queryByText(/−100/)).not.toBeInTheDocument();
  });
});
