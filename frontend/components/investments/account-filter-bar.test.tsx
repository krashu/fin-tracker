import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AccountFilterBar } from "./account-filter-bar";
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

describe("AccountFilterBar", () => {
  it("renders All Accounts pill and all dynamic account tabs with badges", () => {
    const onSelect = vi.fn();
    render(
      <AccountFilterBar
        accounts={mockAccounts}
        selectedAccountId={null}
        onSelectAccount={onSelect}
      />,
    );

    expect(screen.getByText("All Accounts")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument(); // count badge

    expect(screen.getByText("Zerodha Demat")).toBeInTheDocument();
    expect(screen.getByText("Zerodha")).toBeInTheDocument();
    expect(screen.getByText("Demat")).toBeInTheDocument();

    expect(screen.getByText("INDmoney US")).toBeInTheDocument();
    expect(screen.getByText("INDmoney")).toBeInTheDocument();
    expect(screen.getByText("US Broker")).toBeInTheDocument();
    expect(screen.getByText("USD")).toBeInTheDocument();
  });

  it("marks All Accounts as active when selectedAccountId is null", () => {
    const onSelect = vi.fn();
    render(
      <AccountFilterBar
        accounts={mockAccounts}
        selectedAccountId={null}
        onSelectAccount={onSelect}
      />,
    );

    const allButton = screen.getByRole("button", { name: /all accounts/i });
    expect(allButton).toHaveAttribute("aria-pressed", "true");
  });

  it("calls onSelectAccount with account id when clicked", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(
      <AccountFilterBar
        accounts={mockAccounts}
        selectedAccountId={null}
        onSelectAccount={onSelect}
      />,
    );

    const zerodhaTab = screen.getByRole("button", { name: /zerodha demat/i });
    await user.click(zerodhaTab);

    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith(1);
  });

  it("calls onSelectAccount with null when All Accounts is clicked", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(
      <AccountFilterBar
        accounts={mockAccounts}
        selectedAccountId={1}
        onSelectAccount={onSelect}
      />,
    );

    const allTab = screen.getByRole("button", { name: /all accounts/i });
    await user.click(allTab);

    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith(null);
  });

  it("renders loading state when isLoading is true", () => {
    render(
      <AccountFilterBar
        accounts={[]}
        selectedAccountId={null}
        onSelectAccount={vi.fn()}
        isLoading={true}
      />,
    );

    expect(screen.getByText(/loading accounts…/i)).toBeInTheDocument();
  });

  it("defaults to All Accounts active if selectedAccountId is not found in accounts", () => {
    render(
      <AccountFilterBar
        accounts={mockAccounts}
        selectedAccountId={999}
        onSelectAccount={vi.fn()}
      />,
    );

    const allButton = screen.getByRole("button", { name: /all accounts/i });
    expect(allButton).toHaveAttribute("aria-pressed", "true");
  });
});
