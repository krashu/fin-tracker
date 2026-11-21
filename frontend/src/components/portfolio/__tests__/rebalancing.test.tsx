import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, renderWithProviders, screen, waitFor } from "@/tests/helpers/test-utils";
import userEvent from "@testing-library/user-event";
import { AllocationTargetCard } from "@/src/components/portfolio/allocation-target-card";
import { RebalanceSimulationSheet } from "@/src/components/portfolio/rebalance-simulation-sheet";
import * as rebalanceApi from "@/src/api/rebalancing";
import type {
  AllocationTargetRead,
  RebalanceEvaluationRead,
  RebalancePlanRead,
} from "@/src/api/rebalancing";

vi.mock("@/src/api/rebalancing", async (importOriginal) => {
  const actual = await importOriginal<typeof rebalanceApi>();
  return {
    ...actual,
    listAllocationTargets: vi.fn(),
    evaluateRebalancing: vi.fn(),
    batchSetAllocationTargets: vi.fn(),
    planRebalancing: vi.fn(),
  };
});

const mockTargets: AllocationTargetRead[] = [
  {
    id: 1,
    scope_type: "all",
    account_id: null,
    target_type: "asset_class",
    target_key: "equity",
    target_bps: 6000,
    rebalance_threshold_bps: 500,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
  {
    id: 2,
    scope_type: "all",
    account_id: null,
    target_type: "asset_class",
    target_key: "debt",
    target_bps: 4000,
    rebalance_threshold_bps: 500,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
];

const mockEvaluation: RebalanceEvaluationRead = {
  scope_type: "all",
  account_id: null,
  total_portfolio_value_paise: 10000000, // ₹1,00,000
  total_target_bps: 10000,
  is_out_of_tolerance: true,
  unallocated_value_paise: 0,
  unallocated_bps: 0,
  buckets: [
    {
      target_id: 1,
      target_type: "asset_class",
      target_key: "equity",
      target_bps: 6000,
      rebalance_threshold_bps: 500,
      actual_value_paise: 7500000, // 75% (Drift +15%)
      target_value_paise: 6000000,
      actual_bps: 7500,
      drift_bps: 1500,
      drift_paise: 1500000,
      relative_drift_pct: 0.25,
      is_out_of_tolerance: true,
      action: "sell",
      holding_count: 5,
    },
    {
      target_id: 2,
      target_type: "asset_class",
      target_key: "debt",
      target_bps: 4000,
      rebalance_threshold_bps: 500,
      actual_value_paise: 2500000, // 25% (Drift -15%)
      target_value_paise: 4000000,
      actual_bps: 2500,
      drift_bps: -1500,
      drift_paise: -1500000,
      relative_drift_pct: -0.375,
      is_out_of_tolerance: true,
      action: "buy",
      holding_count: 2,
    },
  ],
};

const mockInflowPlan: RebalancePlanRead = {
  mode: "inflow_only",
  scope_type: "all",
  account_id: null,
  total_portfolio_value_paise: 10000000,
  inflow_paise: 2500000, // ₹25,000 fresh inflow
  post_rebalance_portfolio_value_paise: 12500000,
  is_out_of_tolerance: false,
  summary: "Deploying ₹25,000 into debt bucket to eliminate drift.",
  bucket_actions: [
    {
      target_id: 1,
      target_type: "asset_class",
      target_key: "equity",
      target_bps: 6000,
      pre_rebalance_value_paise: 7500000,
      pre_rebalance_bps: 7500,
      recommended_cashflow_paise: 0,
      post_rebalance_value_paise: 7500000,
      post_rebalance_bps: 6000,
      post_rebalance_drift_bps: 0,
    },
    {
      target_id: 2,
      target_type: "asset_class",
      target_key: "debt",
      target_bps: 4000,
      pre_rebalance_value_paise: 2500000,
      pre_rebalance_bps: 2500,
      recommended_cashflow_paise: 2500000,
      post_rebalance_value_paise: 5000000,
      post_rebalance_bps: 4000,
      post_rebalance_drift_bps: 0,
    },
  ],
  recommended_orders: [
    {
      instrument_id: 10,
      symbol: "HDFC-DEBT",
      name: "HDFC Short Term Debt Fund",
      account_id: 1,
      action: "buy",
      amount_paise: 2500000,
      units: "100.5",
      current_nav: "248.75",
      reason: "Deploy fresh cash into underweight debt bucket",
    },
  ],
  lot_sell_recommendations: [],
};

const mockFullPlan: RebalancePlanRead = {
  mode: "full_rebalance",
  scope_type: "all",
  account_id: null,
  total_portfolio_value_paise: 10000000,
  inflow_paise: 0,
  post_rebalance_portfolio_value_paise: 10000000,
  is_out_of_tolerance: false,
  summary: "Sell ₹15,000 of equity lots and deploy into debt.",
  bucket_actions: [
    {
      target_id: 1,
      target_type: "asset_class",
      target_key: "equity",
      target_bps: 6000,
      pre_rebalance_value_paise: 7500000,
      pre_rebalance_bps: 7500,
      recommended_cashflow_paise: -1500000,
      post_rebalance_value_paise: 6000000,
      post_rebalance_bps: 6000,
      post_rebalance_drift_bps: 0,
    },
  ],
  recommended_orders: [
    {
      instrument_id: 5,
      symbol: "INFRA-BEES",
      name: "Nippon India ETF Infra BeES",
      account_id: 1,
      action: "sell",
      amount_paise: 1500000,
      units: "50",
      current_nav: "300.00",
      reason: "Trim overweight equity bucket",
    },
  ],
  lot_sell_recommendations: [
    {
      lot_txn_id: 101,
      instrument_id: 5,
      symbol: "INFRA-BEES",
      name: "Nippon India ETF Infra BeES",
      account_id: 1,
      acquired_on: "2024-05-10",
      units: "50",
      units_to_sell: "50",
      cost_basis_paise: 1200000,
      estimated_proceeds_paise: 1500000,
      estimated_gain_paise: 300000,
      holding_period_days: 560,
      gain_type: "ltcg",
      is_locked: false,
      lock_expiry_date: null,
      tax_drag_rank: 1,
    },
    {
      lot_txn_id: 102,
      instrument_id: 8,
      symbol: "MIRAE-ELSS",
      name: "Mirae Asset ELSS Tax Saver",
      account_id: 1,
      acquired_on: "2025-08-01",
      units: "100",
      units_to_sell: "0",
      cost_basis_paise: 2500000,
      estimated_proceeds_paise: 3000000,
      estimated_gain_paise: 500000,
      holding_period_days: 180,
      gain_type: "stcg",
      is_locked: true,
      lock_expiry_date: "2028-08-01",
      tax_drag_rank: 99,
    },
  ],
};

describe("Target Allocation & Intelligent Rebalancing UI", () => {
  const user = userEvent.setup();

  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe("AllocationTargetCard", () => {
    it("renders empty state when no target allocation is configured", async () => {
      vi.mocked(rebalanceApi.listAllocationTargets).mockResolvedValue([]);
      vi.mocked(rebalanceApi.evaluateRebalancing).mockResolvedValue({
        scope_type: "all",
        account_id: null,
        total_portfolio_value_paise: 10000000,
        total_target_bps: 0,
        is_out_of_tolerance: false,
        unallocated_value_paise: 10000000,
        unallocated_bps: 10000,
        buckets: [],
      });

      renderWithProviders(<AllocationTargetCard />);

      await waitFor(() => {
        expect(
          screen.getByText("No Target Allocation Configured"),
        ).toBeInTheDocument();
      });
      expect(
        screen.getByRole("button", { name: /Set Up Allocation Targets/i }),
      ).toBeInTheDocument();
    });

    it("renders targets, drift indicators and tolerance badges when configured", async () => {
      vi.mocked(rebalanceApi.listAllocationTargets).mockResolvedValue(mockTargets);
      vi.mocked(rebalanceApi.evaluateRebalancing).mockResolvedValue(mockEvaluation);

      renderWithProviders(<AllocationTargetCard />, {
        initialBalanceHidden: false,
      });

      await waitFor(() => {
        expect(screen.getByText("Out of Tolerance")).toBeInTheDocument();
      });

      // Renders bucket rows
      expect(screen.getByText("equity")).toBeInTheDocument();
      expect(screen.getByText("debt")).toBeInTheDocument();

      // Check drift badges
      expect(screen.getByText("+15.0% drift (Overweight)")).toBeInTheDocument();
      expect(screen.getByText("-15.0% drift (Underweight)")).toBeInTheDocument();

      // Check action buttons
      expect(screen.getByRole("button", { name: /Edit Targets/i })).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: /Rebalance Portfolio/i }),
      ).toBeInTheDocument();
    });

    it("opens target config modal and validates sum = 100.00% guard", async () => {
      vi.mocked(rebalanceApi.listAllocationTargets).mockResolvedValue(mockTargets);
      vi.mocked(rebalanceApi.evaluateRebalancing).mockResolvedValue(mockEvaluation);

      renderWithProviders(<AllocationTargetCard />);

      await waitFor(() => {
        expect(screen.getByRole("button", { name: /Edit Targets/i })).toBeInTheDocument();
      });

      // Open config modal
      await user.click(screen.getByRole("button", { name: /Edit Targets/i }));

      expect(
        screen.getByText("Configure Target Allocation"),
      ).toBeInTheDocument();
      expect(
        screen.getByText("Allocation weights sum to exactly 100.00%"),
      ).toBeInTheDocument();

      // Change equity from 60% to 50% so sum becomes 90%
      const weightInputs = screen.getAllByRole("spinbutton", { name: /Target Weight Percentage/i });
      await user.clear(weightInputs[0]);
      await user.type(weightInputs[0], "50");

      await waitFor(() => {
        expect(
          screen.getByText(/Allocation sum is 90.0% \(must equal 100.00% to save\)/i),
        ).toBeInTheDocument();
      });

      // Save button should be disabled
      const saveBtn = screen.getByRole("button", { name: /Save Target Allocation/i });
      expect(saveBtn).toBeDisabled();

      // Click "60/40 Equity-Debt" preset to restore 100%
      const preset6040 = screen.getByText("60/40 Equity-Debt");
      await user.click(preset6040);

      await waitFor(() => {
        expect(
          screen.getByText("Allocation weights sum to exactly 100.00%"),
        ).toBeInTheDocument();
      });
      expect(saveBtn).not.toBeDisabled();
    });
  });

  describe("RebalanceSimulationSheet", () => {
    it("renders Mode A water-filling simulation with cash distribution table", async () => {
      vi.mocked(rebalanceApi.planRebalancing).mockResolvedValue(mockInflowPlan);

      renderWithProviders(
        <RebalanceSimulationSheet
          open={true}
          onOpenChange={() => {}}
          scope="all"
          totalPortfolioValuePaise={10000000}
        />,
        { initialBalanceHidden: false },
      );

      await waitFor(() => {
        expect(
          screen.getByText("Mode A: Inflow-Only (Water-Filling)"),
        ).toBeInTheDocument();
      });

      expect(screen.getByText("Zero Tax Drag")).toBeInTheDocument();
      expect(
        screen.getByText("Fresh Monthly Investment Inflow (₹)"),
      ).toBeInTheDocument();

      // Check water filling table values
      await waitFor(() => {
        expect(screen.getByText("+₹25,000.00")).toBeInTheDocument();
      });
      expect(screen.getByText("HDFC-DEBT")).toBeInTheDocument();
      expect(screen.getByText("BUY")).toBeInTheDocument();
    });

    it("renders Mode B tax-aware full rebalance with ELSS lock-in notice", async () => {
      vi.mocked(rebalanceApi.planRebalancing).mockResolvedValue(mockFullPlan);

      renderWithProviders(
        <RebalanceSimulationSheet
          open={true}
          onOpenChange={() => {}}
          scope="all"
          totalPortfolioValuePaise={10000000}
        />,
        { initialBalanceHidden: false },
      );

      // Switch to Mode B
      const modeBBtn = screen.getByText("Mode B: Tax-Aware Full Rebalance");
      await user.click(modeBBtn);

      await waitFor(() => {
        expect(
          screen.getByText("Statutory Tax & ELSS Guardrails Active"),
        ).toBeInTheDocument();
      });

      // Verify LTCG badge and Locked ELSS unit notice
      await waitFor(() => {
        expect(screen.getByText("LTCG (12.5%)")).toBeInTheDocument();
      });
      expect(screen.getByText("Locked")).toBeInTheDocument();
      expect(screen.getByText("SELL")).toBeInTheDocument();
      expect(screen.getAllByText("INFRA-BEES").length).toBeGreaterThanOrEqual(1);
    });

    it("allows toggling order items in actionable checklist and copying to clipboard", async () => {
      vi.mocked(rebalanceApi.planRebalancing).mockResolvedValue(mockInflowPlan);

      // Mock navigator.clipboard via vi.spyOn
      const writeTextSpy = vi
        .spyOn(navigator.clipboard, "writeText")
        .mockResolvedValue(undefined);

      renderWithProviders(
        <RebalanceSimulationSheet
          open={true}
          onOpenChange={() => {}}
          scope="all"
          totalPortfolioValuePaise={10000000}
        />,
        { initialBalanceHidden: false },
      );

      await waitFor(() => {
        expect(screen.getByText("HDFC-DEBT")).toBeInTheDocument();
      });

      // Find order checkbox and click it
      const checkbox = screen.getByRole("checkbox");
      expect(checkbox).not.toBeChecked();
      await user.click(checkbox);
      expect(checkbox).toBeChecked();

      // Click "Copy Order List"
      const copyBtn = screen.getByRole("button", { name: /Copy Order List/i });
      fireEvent.click(copyBtn);

      expect(writeTextSpy).toHaveBeenCalled();
      await waitFor(() => {
        expect(screen.getByText("Copied!")).toBeInTheDocument();
      });
    });
  });
});
