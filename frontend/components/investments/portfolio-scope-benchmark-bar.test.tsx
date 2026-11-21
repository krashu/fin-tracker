import { describe, expect, it, vi } from "vitest";
import { PortfolioScopeBenchmarkBar } from "./portfolio-scope-benchmark-bar";
import { renderWithProviders, screen, userEvent } from "@/tests/helpers/test-utils";
import type { Benchmark } from "@/lib/api/client";
import type { InvestmentAccountRead } from "@/lib/types/investment-accounts";

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

const mockBenchmarks: Benchmark[] = [
  {
    id: 1,
    name: "Nifty 50",
    kind: "index_fund",
    amfi_code: "120716",
    currency: "INR",
    inception_date: "2013-01-01",
  },
  {
    id: 2,
    name: "S&P 500",
    kind: "index_fund",
    amfi_code: "148381",
    currency: "INR",
    inception_date: "2020-04-28",
  },
];

describe("PortfolioScopeBenchmarkBar", () => {
  it("renders scope pills and benchmark picker", () => {
    renderWithProviders(
      <PortfolioScopeBenchmarkBar
        scope="all"
        selectedAccountId={null}
        accounts={mockAccounts}
        benchmarks={mockBenchmarks}
        selectedBenchmarkId={1}
        onScopeChange={vi.fn()}
        onBenchmarkChange={vi.fn()}
      />,
    );

    expect(screen.getByText("All Holdings")).toBeInTheDocument();
    expect(screen.getByText("Zerodha Demat")).toBeInTheDocument();
    expect(screen.getByText("INDmoney US")).toBeInTheDocument();
    expect(screen.getByText("Family (Self)")).toBeInTheDocument();
    expect(screen.getByText("Nifty 50")).toBeInTheDocument();
  });

  it("handles scope changes when account pills are clicked", async () => {
    const user = userEvent.setup();
    const handleScopeChange = vi.fn();

    renderWithProviders(
      <PortfolioScopeBenchmarkBar
        scope="all"
        selectedAccountId={null}
        accounts={mockAccounts}
        benchmarks={mockBenchmarks}
        selectedBenchmarkId={1}
        onScopeChange={handleScopeChange}
        onBenchmarkChange={vi.fn()}
      />,
    );

    const zerodhaPill = screen.getByText("Zerodha Demat");
    await user.click(zerodhaPill);

    expect(handleScopeChange).toHaveBeenCalledWith("account", 1);

    const allPill = screen.getByText("All Holdings");
    await user.click(allPill);

    expect(handleScopeChange).toHaveBeenCalledWith("all", null);
  });
});
