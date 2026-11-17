import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { AddControls } from "./add-transaction";
import {
  renderWithProviders,
  screen,
  userEvent,
} from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";
import { buildAccount, buildCategory } from "@/tests/helpers/factories";

describe("AddControls entry dialog (app/expenses/add-transaction.tsx)", () => {
  const HINT = /Cashback on a purchase is a rebate/;

  // Only the two queries the dialog makes, with one category each way. `delay: null`
  // drops userEvent's per-keystroke timers: each Radix menu open is slow enough
  // under a loaded worker that the default pacing made a multi-step test flaky.
  async function openAddDialog() {
    server.use(
      http.get("*/api/v1/accounts", () =>
        HttpResponse.json([buildAccount({ id: 1, name: "Salary Account", type: "bank" })]),
      ),
      http.get("*/api/v1/categories", () =>
        HttpResponse.json([
          buildCategory({ id: 7, name: "Cashback", kind: "spend", role: "cashback" }),
          buildCategory({ id: 8, name: "Groceries", kind: "spend" }),
        ]),
      ),
    );
    const user = userEvent.setup({ delay: null });
    renderWithProviders(<AddControls />);
    await user.click(screen.getByRole("button", { name: "Add" }));
    await user.click(await screen.findByRole("button", { name: /Uncategorized/ }));
    return user;
  }

  it("hints Refund / cashback when a spend entry picks the cashback category", async () => {
    const user = await openAddDialog();
    // Spend + Cashback stays legal (a clawback), so nothing is flipped or blocked.
    await user.click(await screen.findByText("Cashback"));
    expect(await screen.findByText(HINT)).toBeInTheDocument();
    expect(screen.getByText("Spend")).toBeInTheDocument();
  });

  it("gives no hint for an ordinary category under Spend", async () => {
    const user = await openAddDialog();
    await user.click(await screen.findByText("Groceries"));
    expect(screen.queryByText(HINT)).toBeNull();
  });

  it("clears the hint when the type switches to Refund / cashback (ADR-0013 rule 1)", async () => {
    const user = await openAddDialog();
    await user.click(await screen.findByText("Cashback"));
    expect(await screen.findByText(HINT)).toBeInTheDocument();

    await user.click(screen.getByText("Spend"));
    await user.click(await screen.findByRole("menuitem", { name: "Refund / cashback" }));
    expect(screen.queryByText(HINT)).toBeNull();
  });
});
