import { describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import userEvent from "@testing-library/user-event";

import { server } from "@/tests/mocks/server";
import { renderWithProviders, screen, waitFor } from "@/tests/helpers/test-utils";
import { NetWorthCard } from "../net-worth-card";
import { CashflowWaterfallCard } from "../cashflow-waterfall-card";

describe("NetWorthCard & CashflowWaterfallCard (frontend/components/dashboard)", () => {
  const mockSummary = {
    as_of: "2026-06-01",
    net_worth_paise: 125000000, // ₹12,50,000
    assets_paise: 130000000,
    liabilities_paise: 5000000,
    delta_30d_paise: 5000000,
    delta_30d_pct: 4.17,
    delta_1y_paise: 25000000,
    delta_1y_pct: 25.0,
    breakdown: {
      bank_cash_paise: 40000000,
      indian_equity_paise: 35000000,
      indian_mf_paise: 35000000,
      us_equity_paise: 10000000,
      debt_fd_paise: 5000000,
      gold_paise: 5000000,
      peer_receivables_paise: 0,
      peer_payables_paise: 5000000,
    },
  };

  const mockTimelinePoints = [
    {
      date: "2026-05-01",
      net_worth_paise: 120000000,
      assets_paise: 125000000,
      liabilities_paise: 5000000,
      bank_cash_paise: 40000000,
      indian_equity_paise: 33000000,
      indian_mf_paise: 34000000,
      us_equity_paise: 9000000,
      debt_fd_paise: 5000000,
      gold_paise: 4000000,
      peer_receivables_paise: 0,
      peer_payables_paise: 5000000,
    },
    {
      date: "2026-06-01",
      net_worth_paise: 125000000,
      assets_paise: 130000000,
      liabilities_paise: 5000000,
      bank_cash_paise: 40000000,
      indian_equity_paise: 35000000,
      indian_mf_paise: 35000000,
      us_equity_paise: 10000000,
      debt_fd_paise: 5000000,
      gold_paise: 5000000,
      peer_receivables_paise: 0,
      peer_payables_paise: 5000000,
    },
  ];

  it("renders headline net worth, delta badges, and period selector", async () => {
    let capturedRange = "1Y";
    server.use(
      http.get("*/api/v1/net-worth/summary", () => HttpResponse.json(mockSummary)),
      http.get("*/api/v1/net-worth/timeline", ({ request }) => {
        const url = new URL(request.url);
        capturedRange = url.searchParams.get("range") || "1Y";
        return HttpResponse.json({
          range: capturedRange,
          interval: "daily",
          start_date: "2026-05-01",
          end_date: "2026-06-01",
          points: mockTimelinePoints,
        });
      }),
    );

    renderWithProviders(<NetWorthCard />, { initialBalanceHidden: false });

    // Eyebrow and Headline value
    expect(await screen.findByText("Net Worth Timeline & Wealth Trajectory")).toBeInTheDocument();
    expect(await screen.findByText("₹12,50,000.00")).toBeInTheDocument();

    // 30d delta
    expect(await screen.findByText("+₹50,000.00")).toBeInTheDocument();
    expect(screen.getByText("(+4.17%)")).toBeInTheDocument();

    // Portfolio link
    expect(screen.getByRole("link", { name: /portfolio/i })).toHaveAttribute("href", "/portfolio");

    // Period pill selector interaction
    const sixMonthBtn = screen.getByRole("button", { name: "6M" });
    await userEvent.click(sixMonthBtn);

    await waitFor(() => {
      expect(capturedRange).toBe("6M");
    });
  });

  it("respects privacy mode balance masking (••••)", async () => {
    server.use(
      http.get("*/api/v1/net-worth/summary", () => HttpResponse.json(mockSummary)),
      http.get("*/api/v1/net-worth/timeline", () =>
        HttpResponse.json({
          range: "1Y",
          interval: "daily",
          start_date: "2026-05-01",
          end_date: "2026-06-01",
          points: mockTimelinePoints,
        }),
      ),
    );

    renderWithProviders(<NetWorthCard />, { initialBalanceHidden: true });

    expect(await screen.findByText("Net Worth Timeline & Wealth Trajectory")).toBeInTheDocument();
    // Wait for masked elements to appear once query resolves
    await waitFor(() => {
      expect(screen.getAllByText("••••").length).toBeGreaterThan(0);
    });
    // The raw headline amount should NOT be in the document
    expect(screen.queryByText("₹12,50,000.00")).not.toBeInTheDocument();
  });

  it("renders Cashflow Waterfall decomposition and allows year toggle", async () => {
    server.use(
      http.get("*/api/v1/dashboards/available-years", () =>
        HttpResponse.json({ years: [2026, 2025] }),
      ),
      http.get("*/api/v1/dashboards/cashflow-waterfall", () =>
        HttpResponse.json({
          year: 2026,
          year_type: "calendar",
          start_date: "2026-01-01",
          end_date: "2026-12-31",
          gross_income_paise: 10000000,
          total_living_expenses_paise: -4000000,
          expense_categories: [
            { category_id: 1, name: "Rent", amount_paise: -3000000, display_color: "#ef4444" },
            { category_id: 2, name: "Food", amount_paise: -1000000, display_color: "#f97316" },
          ],
          peer_net_paise: -500000,
          investment_transfers_paise: -3500000,
          net_liquid_surplus_paise: 2000000,
          steps: [
            { step_index: 1, label: "Gross Income", amount_paise: 10000000, cumulative_paise: 10000000, kind: "income", display_color: "#10b981" },
            { step_index: 2, label: "Rent", amount_paise: -3000000, cumulative_paise: 7000000, kind: "expense", display_color: "#ef4444" },
            { step_index: 3, label: "Food", amount_paise: -1000000, cumulative_paise: 6000000, kind: "expense", display_color: "#f97316" },
            { step_index: 4, label: "Peer Lending / Dues", amount_paise: -500000, cumulative_paise: 5500000, kind: "peer", display_color: "#f59e0b" },
            { step_index: 5, label: "Investment Additions", amount_paise: -3500000, cumulative_paise: 2000000, kind: "investment", display_color: "#3b82f6" },
            { step_index: 6, label: "Net Liquid Surplus", amount_paise: 2000000, cumulative_paise: 2000000, kind: "surplus", display_color: "#06b6d4" },
          ],
        }),
      ),
    );

    renderWithProviders(<CashflowWaterfallCard />, { initialBalanceHidden: false });

    expect(await screen.findByText("Cashflow Waterfall: Inflow to Wealth")).toBeInTheDocument();
    expect(await screen.findByText("₹1,00,000.00")).toBeInTheDocument(); // Income
    expect(screen.getByText("−₹40,000.00")).toBeInTheDocument(); // Living spend
    expect(screen.getByText("₹20,000.00")).toBeInTheDocument(); // Liquid surplus

    // Fiscal year toggle button exists and can be clicked
    const fyBtn = screen.getByRole("button", { name: /Fiscal \(FY\)/i });
    await userEvent.click(fyBtn);
  });
});
