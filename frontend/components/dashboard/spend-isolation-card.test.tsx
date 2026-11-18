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
  fronted_card_usage_paise: 500000,
};

describe("SpendIsolationCard (components/dashboard/spend-isolation-card.tsx)", () => {
  it("renders personal spend isolation breakdown with external overview", async () => {
    renderWithProviders(
      <SpendIsolationCard overview={mockOverview} ready={true} />,
    );

    // Section header
    expect(screen.getByText("Personal Spend Isolation")).toBeInTheDocument();
    expect(screen.getByText("Friends & splits")).toBeInTheDocument();

    // Metrics
    expect(screen.getByText("Gross Debit Spend")).toBeInTheDocument();
    expect(screen.getByText("Statement debits total")).toBeInTheDocument();
    expect(screen.getByText("Fronted for Friends")).toBeInTheDocument();
    expect(screen.getByText("Reimbursable peer splits")).toBeInTheDocument();
    expect(screen.getByText("Net Personal Spend")).toBeInTheDocument();
    expect(screen.getByText("Your true personal burn")).toBeInTheDocument();
  });

  it("fetches overview data from API when external overview is not provided", async () => {
    server.use(
      http.get("*/api/v1/dashboards/overview", () => {
        return HttpResponse.json(mockOverview);
      }),
    );

    renderWithProviders(<SpendIsolationCard period={{ year: 2026, mon: 3 }} />);

    expect(
      await screen.findByText("Personal Spend Isolation"),
    ).toBeInTheDocument();
    expect(await screen.findByText("Gross Debit Spend")).toBeInTheDocument();
    expect(screen.getByText("Fronted for Friends")).toBeInTheDocument();
    expect(screen.getByText("Net Personal Spend")).toBeInTheDocument();
  });

  it("renders 100% personal consumption state cleanly when fronted spend is zero", async () => {
    const zeroOverview: OverviewResponse = {
      ...mockOverview,
      fronted_peer_entries_paise: 0,
      gross_spend_paise: 500000,
      net_personal_spend_paise: 500000,
    };

    renderWithProviders(
      <SpendIsolationCard overview={zeroOverview} ready={true} />,
    );

    expect(
      screen.getByText(
        "100% of debit spending in this period represents personal consumption (no shared friend splits recorded).",
      ),
    ).toBeInTheDocument();
  });
});
