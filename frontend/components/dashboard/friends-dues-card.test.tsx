import { describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";

import { server } from "@/tests/mocks/server";
import { renderWithProviders, screen } from "@/tests/helpers/test-utils";
import { FriendsDuesCard } from "./friends-dues-card";

describe("FriendsDuesCard (components/dashboard/friends-dues-card.tsx)", () => {
  it("auto-collapses (returns null) when there are zero dues and zero fronted expenses", async () => {
    server.use(
      http.get("*/api/v1/friends/summary", () =>
        HttpResponse.json({
          total_receivable_paise: 0,
          total_payable_paise: 0,
          net_balance_paise: 0,
          active_contacts_count: 2,
        }),
      ),
    );

    const { container } = renderWithProviders(
      <FriendsDuesCard
        overview={{
          period: "2026",
          net_worth_paise: 1000000,
          portfolio_value_paise: 500000,
          fx_unavailable_count: 0,
          income_paise: 2000000,
          expense_paise: -800000,
          net_paise: 1200000,
          accounts: [],
          gross_spend_paise: 800000,
          fronted_peer_entries_paise: 0,
          net_personal_spend_paise: 800000,
          fronted_card_usage_paise: 0,
        }}
      />,
    );

    // Should return null (empty container)
    expect(container.firstChild).toBeNull();
  });

  it("renders To Collect and To Pay metrics when active dues exist", async () => {
    server.use(
      http.get("*/api/v1/friends/summary", () =>
        HttpResponse.json({
          total_receivable_paise: 1250000, // ₹12,500
          total_payable_paise: 210000, // ₹2,100
          net_balance_paise: 1040000,
          active_contacts_count: 3,
        }),
      ),
    );

    renderWithProviders(<FriendsDuesCard />, { initialBalanceHidden: false });

    expect(await screen.findByText("Friends & Khata Dues")).toBeInTheDocument();
    expect(screen.getByText("To Collect")).toBeInTheDocument();
    expect(screen.getByText("₹12,500.00")).toBeInTheDocument();
    expect(screen.getByText("To Pay")).toBeInTheDocument();
    expect(screen.getByText("₹2,100.00")).toBeInTheDocument();
  });
});
