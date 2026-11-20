import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { HoldingsView } from "./holdings-view";
import { renderWithProviders, screen } from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";
import type { HoldingRead } from "@/lib/api/client";
import type { InvestmentAccountRead } from "@/lib/types/investment-accounts";

let currentSearchParams = new URLSearchParams();
const mockRouter = { replace: vi.fn(), push: vi.fn() };

vi.mock("next/navigation", () => ({
  useSearchParams: () => currentSearchParams,
  useRouter: () => mockRouter,
  usePathname: () => "/holdings",
}));

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
  {
    id: 2,
    name: "INDmoney US",
    account_type: "us_broker",
    provider: "indmoney",
    external_account_ref: "67890",
    currency: "USD",
    is_active: true,
  },
];

const mockAllHoldings: HoldingRead[] = [
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
];

const mockZerodhaHoldings: HoldingRead[] = [mockAllHoldings[0]];

const mockUsHoldings: HoldingRead[] = [mockAllHoldings[1]];

describe("HoldingsView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    currentSearchParams = new URLSearchParams();
  });

  it("renders filter bar, summary cards, and holdings table for all accounts", async () => {
    server.use(
      http.get("*/api/v1/investment-accounts", () =>
        HttpResponse.json(mockAccounts),
      ),
      http.get("*/api/v1/holdings", () =>
        HttpResponse.json({ holdings: mockAllHoldings }),
      ),
      http.get("*/api/v1/portfolio/summary", () =>
        HttpResponse.json({ holding_xirr: [] }),
      ),
    );

    renderWithProviders(<HoldingsView />, { initialBalanceHidden: false });

    // Filter pills rendered
    expect(await screen.findByText("All Accounts")).toBeInTheDocument();
    expect(await screen.findByText("Zerodha Demat")).toBeInTheDocument();
    expect(await screen.findByText("INDmoney US")).toBeInTheDocument();

    // Summary cards rendered
    expect(screen.getByText("Current Value")).toBeInTheDocument();
    expect(screen.getByText("Total Invested")).toBeInTheDocument();
    expect(screen.getByText("Total Gain/Loss")).toBeInTheDocument();

    // Total rolled-up current value in INR (16,500 + 86,000 = 102,500)
    expect(await screen.findByText("₹1,02,500.00")).toBeInTheDocument();

    // Both holdings in table
    expect(await screen.findByText("INFY")).toBeInTheDocument();
    expect(await screen.findByText("AAPL")).toBeInTheDocument();
  });

  it("clicking an account pill triggers router replace with account_id param", async () => {
    server.use(
      http.get("*/api/v1/investment-accounts", () =>
        HttpResponse.json(mockAccounts),
      ),
      http.get("*/api/v1/holdings", () =>
        HttpResponse.json({ holdings: mockAllHoldings }),
      ),
      http.get("*/api/v1/portfolio/summary", () =>
        HttpResponse.json({ holding_xirr: [] }),
      ),
    );

    const { user } = renderWithProviders(<HoldingsView />, {
      initialBalanceHidden: false,
    });

    const zerodhaTab = await screen.findByRole("button", {
      name: /zerodha demat/i,
    });
    await user.click(zerodhaTab);

    expect(mockRouter.replace).toHaveBeenCalledWith(
      "/holdings?account_id=1",
      { scroll: false },
    );
  });

  it("renders filtered account view when account_id query param is present", async () => {
    currentSearchParams = new URLSearchParams("account_id=2");

    let requestedAccountId: string | null = null;
    server.use(
      http.get("*/api/v1/investment-accounts", () =>
        HttpResponse.json(mockAccounts),
      ),
      http.get("*/api/v1/holdings", ({ request }) => {
        const url = new URL(request.url);
        requestedAccountId = url.searchParams.get("account_id");
        return HttpResponse.json({ holdings: mockUsHoldings });
      }),
      http.get("*/api/v1/portfolio/summary", () =>
        HttpResponse.json({ holding_xirr: [] }),
      ),
    );

    renderWithProviders(<HoldingsView />, { initialBalanceHidden: false });

    // Verify backend received account_id=2
    expect(await screen.findByText("AAPL")).toBeInTheDocument();
    expect(requestedAccountId).toBe("2");

    // US Broker account displays native USD amounts in summary card and table
    const currentValues = await screen.findAllByText("$1,000.00");
    expect(currentValues.length).toBeGreaterThanOrEqual(1);

    const investedValues = await screen.findAllByText("$900.00");
    expect(investedValues.length).toBeGreaterThanOrEqual(1);

    const pnlValues = await screen.findAllByText("+$100.00");
    expect(pnlValues.length).toBeGreaterThanOrEqual(1);

    // Dual-currency INR equivalents are displayed
    expect(await screen.findByText("≈ ₹86,000.00")).toBeInTheDocument();
    expect(await screen.findByText("≈ ₹74,700.00")).toBeInTheDocument();
  });

  it("clears account filter when All Accounts is clicked", async () => {
    currentSearchParams = new URLSearchParams("account_id=1");

    server.use(
      http.get("*/api/v1/investment-accounts", () =>
        HttpResponse.json(mockAccounts),
      ),
      http.get("*/api/v1/holdings", () =>
        HttpResponse.json({ holdings: mockZerodhaHoldings }),
      ),
      http.get("*/api/v1/portfolio/summary", () =>
        HttpResponse.json({ holding_xirr: [] }),
      ),
    );

    const { user } = renderWithProviders(<HoldingsView />, {
      initialBalanceHidden: false,
    });

    const allTab = await screen.findByRole("button", {
      name: /all accounts/i,
    });
    await user.click(allTab);

    expect(mockRouter.replace).toHaveBeenCalledWith("/holdings", {
      scroll: false,
    });
  });
});
