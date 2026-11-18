import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { SpendIsolationCard } from "./spend-isolation-card";
import { renderWithProviders, screen } from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";
import type { OverviewResponse } from "@/lib/api/client";

const mockOverview: OverviewResponse = {
  period: "2026-03",
  net_worth_paise: 50000000,
  portfolio_value_paise: 30000000,
  fx_unavailable_count: 0,
  income_paise: 20000000,
  expense_paise: -700000,
  net_paise: 19300000,
  accounts: [],
  gross_spend_paise: 700000, // ₹7,000
  fronted_peer_entries_paise: 300000, // ₹3,000
  net_personal_spend_paise: 400000, // ₹4,000
  fronted_card_usage_paise: 500000, // ₹5,000
};

describe("SpendIsolationCard (components/dashboard/spend-isolation-card.tsx)", () => {
  it("renders card usage metric and personal spend isolation breakdown with external overview", async () => {
    renderWithProviders(
      <SpendIsolationCard overview={mockOverview} ready={true} />,
    );

    // Section header
    expect(screen.getByText("Friends & Spend Isolation")).toBeInTheDocument();
    expect(screen.getByText("Friends & splits")).toBeInTheDocument();

    // Tile 1: Card Used by Friends this Month
    expect(
      screen.getByText("Card Used by Friends this Month"),
    ).toBeInTheDocument();
    // Default privacy masking in test environment displays masked text or formatted value
    expect(
      screen.getByText("Credit card debits fronted for friends this month."),
    ).toBeInTheDocument();

    // Tile 2: Personal Spend Isolation Breakdown
    expect(screen.getByText("Personal Spend Isolation")).toBeInTheDocument();
    expect(screen.getByText("Gross Spend")).toBeInTheDocument();
    expect(screen.getByText("Fronted for Friends")).toBeInTheDocument();
    expect(screen.getByText("Net Personal")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Net Personal Spend = Gross Spend − Fronted for Friends. Unpaired transfers stay excluded.",
      ),
    ).toBeInTheDocument();
  });

  it("fetches overview data from API when external overview is not provided", async () => {
    server.use(
      http.get("*/api/v1/dashboards/overview", () => {
        return HttpResponse.json(mockOverview);
      }),
    );

    renderWithProviders(<SpendIsolationCard period={{ year: 2026, mon: 3 }} />);

    expect(
      await screen.findByText("Card Used by Friends this Month"),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("Personal Spend Isolation"),
    ).toBeInTheDocument();
    expect(screen.getByText("Gross Spend")).toBeInTheDocument();
    expect(screen.getByText("Fronted for Friends")).toBeInTheDocument();
    expect(screen.getByText("Net Personal")).toBeInTheDocument();
  });

  it("renders zero state gracefully when fronted card usage is zero", async () => {
    const zeroOverview: OverviewResponse = {
      ...mockOverview,
      fronted_card_usage_paise: 0,
      fronted_peer_entries_paise: 0,
      gross_spend_paise: 0,
      net_personal_spend_paise: 0,
    };

    renderWithProviders(
      <SpendIsolationCard overview={zeroOverview} ready={true} />,
    );

    expect(
      screen.getByText("No card debits fronted for friends this month."),
    ).toBeInTheDocument();
  });
});
