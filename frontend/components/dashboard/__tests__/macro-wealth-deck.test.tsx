import { describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import userEvent from "@testing-library/user-event";

import { server } from "@/tests/mocks/server";
import { renderWithProviders, screen, waitFor } from "@/tests/helpers/test-utils";
import { MacroWealthDeck } from "../macro-wealth-deck";

describe("MacroWealthDeck (frontend/components/dashboard)", () => {
  const mockSummary = {
    as_of: "2026-06-01",
    net_worth_paise: 125000000,
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

  const mockWaterfall = {
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
  };

  it("renders Two-Tier Header Layout with default Net Worth Trajectory view", async () => {
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
      http.get("*/api/v1/dashboards/available-years", () =>
        HttpResponse.json({ years: [2026, 2025] }),
      ),
      http.get("*/api/v1/dashboards/cashflow-waterfall", () =>
        HttpResponse.json(mockWaterfall),
      ),
    );

    renderWithProviders(<MacroWealthDeck />, { initialBalanceHidden: false });

    // Tier-1: Primary deck selector tabs
    expect(screen.getByRole("tab", { name: /Net Worth Trajectory/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Cashflow Waterfall/i })).toBeInTheDocument();

    // Portfolio link in top tier
    expect(screen.getByRole("link", { name: /Portfolio analytics/i })).toHaveAttribute("href", "/portfolio");

    // Tier-2 sub-row controls for Net Worth
    expect(screen.getByRole("button", { name: "Total" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Breakdown" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "1M" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "1Y" })).toBeInTheDocument();

    // Embedded child card should NOT render duplicate outer title
    expect(screen.queryByText("Net Worth Timeline & Wealth Trajectory")).not.toBeInTheDocument();

    // But headline metrics SHOULD be present
    expect(await screen.findByText("₹12,50,000.00")).toBeInTheDocument();
    expect(await screen.findByText("+₹50,000.00")).toBeInTheDocument();
  });

  it("toggles between Total and Breakdown view composition", async () => {
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
      http.get("*/api/v1/dashboards/available-years", () =>
        HttpResponse.json({ years: [2026, 2025] }),
      ),
      http.get("*/api/v1/dashboards/cashflow-waterfall", () =>
        HttpResponse.json(mockWaterfall),
      ),
    );

    renderWithProviders(<MacroWealthDeck />, { initialBalanceHidden: false });

    const totalBtn = screen.getByRole("button", { name: "Total" });
    const breakdownBtn = screen.getByRole("button", { name: "Breakdown" });

    // Total is active by default
    expect(totalBtn.className).toContain("bg-background");
    expect(totalBtn).toHaveAttribute("aria-pressed", "true");
    expect(breakdownBtn).toHaveAttribute("aria-pressed", "false");

    // Switch to Breakdown
    await userEvent.click(breakdownBtn);
    expect(breakdownBtn.className).toContain("bg-background");
    expect(breakdownBtn).toHaveAttribute("aria-pressed", "true");
    expect(totalBtn).toHaveAttribute("aria-pressed", "false");

    // Switch back to Total
    await userEvent.click(totalBtn);
    expect(totalBtn.className).toContain("bg-background");
    expect(totalBtn).toHaveAttribute("aria-pressed", "true");
    expect(breakdownBtn).toHaveAttribute("aria-pressed", "false");
  });

  it("switches to Cashflow Waterfall tab and updates Tier-2 controls", async () => {
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
      http.get("*/api/v1/dashboards/available-years", () =>
        HttpResponse.json({ years: [2026, 2025] }),
      ),
      http.get("*/api/v1/dashboards/cashflow-waterfall", () =>
        HttpResponse.json(mockWaterfall),
      ),
    );

    renderWithProviders(<MacroWealthDeck />, { initialBalanceHidden: false });

    // Click Cashflow Waterfall tab
    const waterfallTab = screen.getByRole("tab", { name: /Cashflow Waterfall/i });
    await userEvent.click(waterfallTab);

    // Tier-2 controls now show CY/FY and Year selector
    const cyBtn = await screen.findByRole("button", { name: "Calendar (CY)" });
    const fyBtn = screen.getByRole("button", { name: "Fiscal (FY)" });
    expect(cyBtn).toBeInTheDocument();
    expect(cyBtn).toHaveAttribute("aria-pressed", "true");
    expect(fyBtn).toBeInTheDocument();
    expect(fyBtn).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("combobox")).toBeInTheDocument();

    // Range buttons from trajectory should no longer be visible
    expect(screen.queryByRole("button", { name: "1M" })).not.toBeInTheDocument();

    // Duplicate waterfall title should NOT be present (embedded suppression)
    expect(screen.queryByText("Cashflow Waterfall: Inflow to Wealth")).not.toBeInTheDocument();

    // Waterfall summary strip numbers should be present
    expect(await screen.findByText("₹1,00,000.00")).toBeInTheDocument();
    expect(screen.getByText("−₹40,000.00")).toBeInTheDocument();
  });

  it("respects privacy mode balance masking across deck views", async () => {
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
      http.get("*/api/v1/dashboards/available-years", () =>
        HttpResponse.json({ years: [2026, 2025] }),
      ),
      http.get("*/api/v1/dashboards/cashflow-waterfall", () =>
        HttpResponse.json(mockWaterfall),
      ),
    );

    const { container } = renderWithProviders(<MacroWealthDeck />, { initialBalanceHidden: true });

    // Verify masked bullets appear and raw headline net worth is hidden
    await waitFor(() => {
      expect(screen.getAllByText("••••").length).toBeGreaterThan(0);
    });
    expect(screen.queryByText("₹12,50,000.00")).not.toBeInTheDocument();

    // Verify ChartContainer has blur-sm and pointer-events-none applied
    const chartContainer = container.querySelector('[data-slot="chart"]');
    expect(chartContainer).toBeInTheDocument();
    expect(chartContainer?.className).toContain("blur-sm");
    expect(chartContainer?.className).toContain("pointer-events-none");
  });
});
