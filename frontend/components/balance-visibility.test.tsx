import { describe, expect, it } from "vitest";
import {
  Sensitive,
  useBalanceHidden,
} from "@/components/balance-visibility";
import { act, renderWithProviders, screen } from "@/tests/helpers/test-utils";

function TestToggleComponent() {
  const { hidden, toggle } = useBalanceHidden();
  return (
    <div>
      <span data-testid="status">{hidden ? "HIDDEN" : "VISIBLE"}</span>
      <button onClick={toggle}>Toggle Balance</button>
      <Sensitive>₹50,000.00</Sensitive>
    </div>
  );
}

describe("Sensitive & BalanceVisibilityProvider", () => {
  it("renders masked placeholder •••• by default for privacy", () => {
    renderWithProviders(<Sensitive>₹1,23,456.78</Sensitive>, {
      initialBalanceHidden: true,
    });

    expect(screen.getByLabelText("Amount hidden")).toBeInTheDocument();
    expect(screen.getByText("••••")).toBeInTheDocument();
    expect(screen.queryByText("₹1,23,456.78")).not.toBeInTheDocument();
  });

  it("renders actual amount when balance is visible", () => {
    renderWithProviders(<Sensitive>₹1,23,456.78</Sensitive>, {
      initialBalanceHidden: false,
    });

    expect(screen.getByText("₹1,23,456.78")).toBeInTheDocument();
    expect(screen.queryByText("••••")).not.toBeInTheDocument();
  });

  it("toggles visibility and updates localStorage", async () => {
    const { user } = renderWithProviders(<TestToggleComponent />, {
      initialBalanceHidden: true,
    });

    expect(screen.getByTestId("status")).toHaveTextContent("HIDDEN");
    expect(screen.getByText("••••")).toBeInTheDocument();

    // Click toggle button
    await user.click(screen.getByRole("button", { name: /toggle balance/i }));

    expect(screen.getByTestId("status")).toHaveTextContent("VISIBLE");
    expect(screen.getByText("₹50,000.00")).toBeInTheDocument();
    expect(window.localStorage.getItem("balance-hidden")).toBe("0");

    // Click toggle button again
    await user.click(screen.getByRole("button", { name: /toggle balance/i }));

    expect(screen.getByTestId("status")).toHaveTextContent("HIDDEN");
    expect(screen.getByText("••••")).toBeInTheDocument();
    expect(window.localStorage.getItem("balance-hidden")).toBe("1");
  });
});
