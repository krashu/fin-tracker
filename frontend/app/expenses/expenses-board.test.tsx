import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { ExpensesBoard } from "./expenses-board";
import { renderWithProviders, screen, waitFor, within } from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";
import { buildCategory, buildTransaction } from "@/tests/helpers/factories";

// Decouple from the board's header widgets (the month navigator, summary strip and filter
// row): none is under test, and rendering them costs most of this file's run time.
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
  useRouter: () => ({ push: vi.fn() }),
}));
vi.mock("./filter-row", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./filter-row")>()),
  FilterRow: () => null,
}));
vi.mock("@/components/dashboard/summary-strip", () => ({ SummaryStrip: () => null }));
vi.mock("@/components/dashboard/use-available-years", () => ({
  useAvailableYears: () => ({ years: [2026] }),
}));

const categories = [
  buildCategory({ id: 1, name: "Food & Dining", kind: "spend" }),
  buildCategory({ id: 2, name: "Groceries", kind: "spend", parent_id: 1 }),
];
const swiggy = buildTransaction({
  id: 1,
  account_id: 2,
  merchant_raw: "SWIGGY BANGALORE",
  category_id: 1,
});
const zomato = buildTransaction({
  id: 2,
  account_id: 2,
  merchant_raw: "ZOMATO",
  category_id: 1,
});

function useBoardHandlers(rows = [swiggy]) {
  server.use(
    http.get("*/api/v1/transactions", () => HttpResponse.json(rows)),
    http.get("*/api/v1/categories", () => HttpResponse.json(categories)),
  );
}

async function correctSwiggyToGroceries(user: ReturnType<typeof renderWithProviders>["user"]) {
  // Label and text queries, and the dialog scope, keep the lookups off the whole-page
  // accessibility tree, which is what made this file slow.
  await user.click(await screen.findByLabelText("Edit SWIGGY BANGALORE"));
  const dialog = await screen.findByRole("dialog");
  await user.click(within(dialog).getByText("Food & Dining"));
  await user.click(await screen.findByText("Groceries"));
  await user.click(within(dialog).getByText("Save"));
}

describe("ExpensesBoard (app/expenses/expenses-board.tsx)", () => {
  it("offers to apply a category correction to recent rows of the same merchant", async () => {
    useBoardHandlers();
    const bulkBodies: unknown[] = [];
    server.use(
      http.get("*/api/v1/transactions/1/past-rows", () =>
        HttpResponse.json({ merchant: "swiggy", category_id: 2, transaction_ids: [7, 8] }),
      ),
      http.post("*/api/v1/transactions/bulk-categorise", async ({ request }) => {
        bulkBodies.push(await request.json());
        return HttpResponse.json({ updated: 2, taught: 1 });
      }),
    );
    const { user } = renderWithProviders(<ExpensesBoard />);

    await correctSwiggyToGroceries(user);

    expect(await screen.findByText(/2 recent swiggy rows/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(bulkBodies).toEqual([{ transaction_ids: [7, 8], category_id: 2 }]));
    await waitFor(() =>
      expect(screen.queryByRole("region", { name: "Apply to past rows" })).not.toBeInTheDocument(),
    );
  });

  it("shows no prompt when the correction has no past rows", async () => {
    useBoardHandlers();
    let asked = 0;
    server.use(
      http.get("*/api/v1/transactions/1/past-rows", () => {
        asked += 1;
        return HttpResponse.json({ merchant: "swiggy", category_id: 2, transaction_ids: [] });
      }),
    );
    const { user } = renderWithProviders(<ExpensesBoard />);

    await correctSwiggyToGroceries(user);

    await waitFor(() => expect(asked).toBe(1));
    expect(screen.queryByRole("region", { name: "Apply to past rows" })).not.toBeInTheDocument();
    expect(screen.queryByText(/recent swiggy/)).not.toBeInTheDocument();
  });

  it("a later board write never refetches or resurfaces the prompt", async () => {
    useBoardHandlers([swiggy, zomato]);
    let asked = 0;
    let answer = { merchant: "swiggy", category_id: 2, transaction_ids: [7, 8] };
    server.use(
      http.get("*/api/v1/transactions/1/past-rows", () => {
        asked += 1;
        return HttpResponse.json(answer);
      }),
    );
    const { user } = renderWithProviders(<ExpensesBoard />);

    await correctSwiggyToGroceries(user);
    expect(await screen.findByText(/2 recent swiggy rows/)).toBeInTheDocument();
    // The data moves on after the snapshot: a refetch would now say 1 row.
    answer = { ...answer, transaction_ids: [7] };

    await user.click(await screen.findByRole("checkbox", { name: "Select SWIGGY BANGALORE" }));
    await user.click(screen.getByRole("checkbox", { name: "Select ZOMATO" }));
    await user.click(await screen.findByRole("button", { name: "Categorize" }));
    await user.click(await screen.findByText("Groceries"));
    await waitFor(() =>
      expect(screen.queryByRole("toolbar", { name: "Bulk actions" })).not.toBeInTheDocument(),
    );

    expect(asked).toBe(1);
    expect(screen.getByText(/2 recent swiggy rows/)).toBeInTheDocument();
  });

  it("does not resurface an empty answer after a later board write", async () => {
    useBoardHandlers([swiggy, zomato]);
    let asked = 0;
    server.use(
      http.get("*/api/v1/transactions/1/past-rows", () => {
        asked += 1;
        return HttpResponse.json({ merchant: "swiggy", category_id: 2, transaction_ids: [] });
      }),
    );
    const { user } = renderWithProviders(<ExpensesBoard />);

    await correctSwiggyToGroceries(user);
    await waitFor(() => expect(asked).toBe(1));
    await user.click(await screen.findByRole("checkbox", { name: "Select ZOMATO" }));
    await user.click(await screen.findByRole("button", { name: "Categorize" }));
    await user.click(await screen.findByText("Groceries"));
    await waitFor(() =>
      expect(screen.queryByRole("toolbar", { name: "Bulk actions" })).not.toBeInTheDocument(),
    );

    expect(asked).toBe(1);
    expect(screen.queryByRole("region", { name: "Apply to past rows" })).not.toBeInTheDocument();
  });
});
