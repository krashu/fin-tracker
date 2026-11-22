import { describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";

import { server } from "@/tests/mocks/server";
import { renderWithProviders, screen } from "@/tests/helpers/test-utils";
import { DailyPulseRibbon } from "../daily-pulse-ribbon";
import { MacroWealthDeck } from "../macro-wealth-deck";

describe("DailyPulseRibbon & MacroWealthDeck (frontend/components/dashboard)", () => {
  const mockSummary = {
    as_of: "2026-11-22",
    net_worth_paise: 160374514, // ₹16,03,745.14
    assets_paise: 160574514,
    liabilities_paise: 200000,
    delta_30d_paise: 4200000,
    delta_30d_pct: 2.7,
    delta_1y_paise: 21000000,
    delta_1y_pct: 15.1,
    breakdown: {
      bank_cash_paise: 42300000,
      indian_equity_paise: 35000000,
      indian_mf_paise: 45000000,
      us_equity_paise: 18000000,
      debt_fd_paise: 19574514,
      gold_paise: 500000,
      peer_receivables_paise: 450000,
      peer_payables_paise: 200000,
    },
  };

  const mockOverview = {
    period: "2026",
    net_worth_paise: 160374514,
    portfolio_value_paise: 117574514,
    fx_unavailable_count: 0,
    income_paise: 15000000,
    expense_paise: -6543200,
    net_paise: 8456800,
    accounts: [
      {
        account_id: 1,
        name: "HDFC Salary",
        type: "bank" as const,
        currency: "INR" as const,
        balance_paise: 38500000,
        spend_ytd_paise: null,
        archived: false,
      },
      {
        account_id: 2,
        name: "Cash Wallet",
        type: "cash" as const,
        currency: "INR" as const,
        balance_paise: 3800000,
        spend_ytd_paise: null,
        archived: false,
      },
    ],
    gross_spend_paise: -6543200,
    fronted_peer_entries_paise: 0,
    net_personal_spend_paise: -6543200,
    fronted_card_usage_paise: 0,
    peer_receivables_paise: 450000,
    peer_payables_paise: 200000,
  };

  it("renders all 4 glanceable tiles in DailyPulseRibbon", async () => {
    server.use(
      http.get("*/api/v1/net-worth/summary", () => HttpResponse.json(mockSummary)),
      http.get("*/api/v1/imports/pending", () =>
        HttpResponse.json([{ batch_id: 1, pending_count: 3 }]),
      ),
      http.get("*/api/v1/dashboards/period-totals", () =>
        HttpResponse.json({
          expense_paise: -6543200,
          income_paise: 15000000,
        }),
      ),
    );

    renderWithProviders(<DailyPulseRibbon overview={mockOverview} />, {
      initialBalanceHidden: false,
    });

    // Tile 1: Net Worth
    expect(await screen.findByText("Total Net Worth")).toBeInTheDocument();
    expect(await screen.findByText("₹16,03,745.14")).toBeInTheDocument();
    expect(screen.getByText("+2.7% (30d)")).toBeInTheDocument();

    // Tile 2: Working Capital
    expect(screen.getByText("Working Capital")).toBeInTheDocument();
    expect(screen.getByText("₹4,23,000.00")).toBeInTheDocument();
    expect(screen.getByText("Safe to spend")).toBeInTheDocument();

    // Tile 3: Spend pacing
    expect(screen.getByText(/Spend ·/i)).toBeInTheDocument();
    expect(screen.getByText("₹65,432.00")).toBeInTheDocument();

    // Tile 4: Actions & Hub
    expect(screen.getByText("Actions & Attention")).toBeInTheDocument();
    expect(await screen.findByText("3 imports to review")).toBeInTheDocument();
    expect(screen.getByText(/Collect/i)).toBeInTheDocument();
  });

  it("renders MacroWealthDeck with segmented tabs and embedded visualizers", async () => {
    server.use(
      http.get("*/api/v1/net-worth/summary", () => HttpResponse.json(mockSummary)),
      http.get("*/api/v1/net-worth/timeline", () =>
        HttpResponse.json({
          range: "1Y",
          interval: "monthly",
          start_date: "2025-11-01",
          end_date: "2026-11-22",
          points: [],
        }),
      ),
    );

    renderWithProviders(<MacroWealthDeck overview={mockOverview} />, {
      initialBalanceHidden: false,
    });

    expect(screen.getByRole("tab", { name: /Net Worth Trajectory/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Cashflow Waterfall/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /portfolio analytics/i })).toHaveAttribute(
      "href",
      "/portfolio",
    );
  });
});
