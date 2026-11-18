import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { TransactionDialog } from "./transaction-dialog";
import { renderWithProviders, screen, waitFor } from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";
import {
  buildAccount,
  buildCategory,
  buildTransaction,
} from "@/tests/helpers/factories";
import type { TransactionUpdate } from "@/lib/api/client";

describe("TransactionDialog (app/expenses/transaction-dialog.tsx)", () => {
  const mockAccounts = [
    buildAccount({ id: 1, name: "Salary Account", type: "bank" }),
    buildAccount({ id: 2, name: "Amazon Pay CC", type: "credit_card" }),
  ];

  const mockCategories = [
    buildCategory({ id: 1, name: "Food & Dining", kind: "spend" }),
    buildCategory({ id: 2, name: "Groceries", kind: "spend", parent_id: 1 }),
    buildCategory({ id: 3, name: "Salary", kind: "income" }),
  ];

  it("renders transaction details and disables Save when untouched", () => {
    const txn = buildTransaction({
      amount_paise: -50000, // ₹500.00
      merchant_raw: "Swiggy Bangalore",
      category_id: 1,
      category_name: "Food & Dining",
    });

    renderWithProviders(
      <TransactionDialog
        txn={txn}
        categories={mockCategories}
        accounts={mockAccounts}
        accountLabelText="Salary Account"
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByDisplayValue("Swiggy Bangalore")).toBeInTheDocument();
    expect(screen.getByDisplayValue("500.00")).toBeInTheDocument();

    const saveButton = screen.getByRole("button", { name: "Save" });
    expect(saveButton).toBeDisabled();
  });

  it("enforces minimal PATCH sending only changed fields on category edit", async () => {
    const txn = buildTransaction({
      id: 42,
      amount_paise: -50000,
      merchant_raw: "Swiggy Bangalore",
      category_id: 1,
    });

    let patchedPayload: TransactionUpdate | null = null;
    server.use(
      http.patch("*/api/v1/transactions/42", async ({ request }) => {
        patchedPayload = (await request.json()) as TransactionUpdate;
        return HttpResponse.json({ ...txn, ...patchedPayload });
      }),
    );

    const onClose = vi.fn();
    const { user, queryClient } = renderWithProviders(
      <TransactionDialog
        txn={txn}
        categories={mockCategories}
        accounts={mockAccounts}
        accountLabelText="Salary Account"
        onClose={onClose}
      />,
    );
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    // Open category selector and change to Groceries (id: 2)
    const categoryTrigger = screen.getByRole("button", {
      name: /Food & Dining/,
    });
    await user.click(categoryTrigger);

    const groceriesOption = await screen.findByText("Groceries");
    await user.click(groceriesOption);

    const saveButton = screen.getByRole("button", { name: "Save" });
    expect(saveButton).toBeEnabled();
    await user.click(saveButton);

    await waitFor(() => {
      expect(onClose).toHaveBeenCalled();
    });

    // Minimal PATCH discipline: only category_id is sent
    expect(patchedPayload).toEqual({
      category_id: 2,
    });

    // Cache invalidation verified
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["transactions"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["dashboards"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["labels"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["rules"] });
  });

  it("handles ADR-0009 refund sign: spend with positive amount_paise", async () => {
    const txn = buildTransaction({
      id: 43,
      amount_paise: -25000, // ₹250.00 spend
      transaction_type: "spend",
    });

    let patchedPayload: TransactionUpdate | null = null;
    server.use(
      http.patch("*/api/v1/transactions/43", async ({ request }) => {
        patchedPayload = (await request.json()) as TransactionUpdate;
        return HttpResponse.json({ ...txn, ...patchedPayload });
      }),
    );

    const { user } = renderWithProviders(
      <TransactionDialog
        txn={txn}
        categories={mockCategories}
        accounts={mockAccounts}
        accountLabelText="Salary Account"
        onClose={vi.fn()}
      />,
    );

    // Open Direction dropdown (Spend / Refund / cashback / Income)
    const directionTrigger = screen.getByText("Spend");
    await user.click(directionTrigger);

    // Select Refund (ADR-0009: spend row with positive amount_paise)
    const refundOption = await screen.findByText("Refund / cashback");
    await user.click(refundOption);

    const saveButton = screen.getByRole("button", { name: "Save" });
    await user.click(saveButton);

    await waitFor(() => {
      expect(patchedPayload).not.toBeNull();
    });

    // Positive paise for refund
    expect(patchedPayload).toEqual({
      amount_paise: 25000,
    });
  });

  it("keeps an income reversal negative when only its category changes", async () => {
    // ADR-0009 rule 12: a negative income row is an income reversal. The dialog
    // must seed that direction, not re-sign the amount to +, or a category-only
    // save would PATCH +amount.
    const txn = buildTransaction({
      id: 44,
      transaction_type: "income",
      amount_paise: -50000,
      category_id: 3,
    });
    const categories = [
      ...mockCategories,
      buildCategory({ id: 4, name: "Bonus", kind: "income" }),
    ];

    let patchedPayload: TransactionUpdate | null = null;
    server.use(
      http.patch("*/api/v1/transactions/44", async ({ request }) => {
        patchedPayload = (await request.json()) as TransactionUpdate;
        return HttpResponse.json({ ...txn, ...patchedPayload });
      }),
    );

    const { user } = renderWithProviders(
      <TransactionDialog
        txn={txn}
        categories={categories}
        accounts={mockAccounts}
        accountLabelText="Salary Account"
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText("Income reversal")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^Salary$/ }));
    await user.click(await screen.findByText("Bonus"));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(patchedPayload).not.toBeNull();
    });
    expect(patchedPayload).toEqual({ category_id: 4 });
  });

  it("retypes an unpaired row to Transfer, keeping its sign", async () => {
    // ADR-0002 rule 7d. The stored sign survives (the amount is not re-derived),
    // a spend category rides along (transfer shares the spend scope), and the
    // dialog says what a transfer means for the totals.
    const txn = buildTransaction({
      id: 45,
      transaction_type: "spend",
      amount_paise: -50000,
      category_id: 1,
    });

    let patchedPayload: TransactionUpdate | null = null;
    server.use(
      http.patch("*/api/v1/transactions/45", async ({ request }) => {
        patchedPayload = (await request.json()) as TransactionUpdate;
        return HttpResponse.json({ ...txn, ...patchedPayload });
      }),
    );

    const { user } = renderWithProviders(
      <TransactionDialog
        txn={txn}
        categories={mockCategories}
        accounts={mockAccounts}
        accountLabelText="Salary Account"
        onClose={vi.fn()}
      />,
    );

    expect(
      screen.queryByText("Transfers count in neither spend nor income."),
    ).not.toBeInTheDocument();

    await user.click(screen.getByText("Spend"));
    await user.click(await screen.findByRole("menuitem", { name: "Transfer" }));

    expect(
      screen.getByText("Transfers count in neither spend nor income."),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(patchedPayload).not.toBeNull();
    });
    expect(patchedPayload).toEqual({ transaction_type: "transfer" });
  });

  it("clears an income category when retyped to Transfer", async () => {
    // Income and transfer sit in different category scopes, so the kind flip
    // must carry an explicit null (ADR-0006 rule 13) rather than a 422.
    const txn = buildTransaction({
      id: 46,
      transaction_type: "income",
      amount_paise: 5000,
      category_id: 3,
    });

    let patchedPayload: TransactionUpdate | null = null;
    server.use(
      http.patch("*/api/v1/transactions/46", async ({ request }) => {
        patchedPayload = (await request.json()) as TransactionUpdate;
        return HttpResponse.json({ ...txn, ...patchedPayload });
      }),
    );

    const { user } = renderWithProviders(
      <TransactionDialog
        txn={txn}
        categories={mockCategories}
        accounts={mockAccounts}
        accountLabelText="Salary Account"
        onClose={vi.fn()}
      />,
    );

    await user.click(screen.getByText("Income"));
    await user.click(await screen.findByRole("menuitem", { name: "Transfer" }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(patchedPayload).not.toBeNull();
    });
    expect(patchedPayload).toEqual({
      transaction_type: "transfer",
      category_id: null,
    });
  });

  it("handles linked transfer pairs and unlinking (ADR-0002)", async () => {
    const pairedTxn = buildTransaction({
      id: 88,
      amount_paise: -1500000,
      transaction_type: "transfer",
      transfer_pair_id: 999,
    });

    const onClose = vi.fn();
    let unlinkCalled = false;
    server.use(
      http.post("*/api/v1/transactions/88/unlink", () => {
        unlinkCalled = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );

    const { user, queryClient } = renderWithProviders(
      <TransactionDialog
        txn={pairedTxn}
        categories={mockCategories}
        accounts={mockAccounts}
        accountLabelText="Salary Account"
        onClose={onClose}
      />,
    );
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    // Banner should be present for linked transfer
    expect(screen.getByText("Linked CC bill payment.")).toBeInTheDocument();
    const breakLinkButton = screen.getByRole("button", { name: "Break link" });

    await user.click(breakLinkButton);

    await waitFor(() => {
      expect(unlinkCalled).toBe(true);
    });

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["transactions"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["dashboards"] });
    // The restored type and category are server-side only, so the dialog closes.
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("links a refund to an original picked from its candidates", async () => {
    const refund = buildTransaction({ id: 50, amount_paise: 25000, transaction_type: "spend" });
    const original = buildTransaction({
      id: 7,
      amount_paise: -25000,
      merchant_raw: "AMAZON",
      date: "2026-08-01",
    });
    let body: unknown = null;
    server.use(
      http.get("*/api/v1/transactions/50/refund-candidates", () => HttpResponse.json([original])),
      http.put("*/api/v1/transactions/50/refund-link", async ({ request }) => {
        body = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
    );

    const { user, queryClient } = renderWithProviders(
      <TransactionDialog
        txn={refund}
        categories={mockCategories}
        accounts={mockAccounts}
        accountLabelText="Salary Account"
        onClose={vi.fn()}
      />,
    );
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    await user.click(screen.getByRole("button", { name: /Not linked/ }));
    await user.click(await screen.findByText(/AMAZON/));

    await waitFor(() => expect(body).toEqual({ original_id: 7 }));
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["transactions"] });
    expect(await screen.findByRole("button", { name: /AMAZON/ })).toBeInTheDocument();
  });

  it("removes a refund link", async () => {
    const refund = buildTransaction({
      id: 51,
      amount_paise: 25000,
      transaction_type: "spend",
      refund_of_id: 7,
    });
    let deleted = false;
    server.use(
      http.delete("*/api/v1/transactions/51/refund-link", () => {
        deleted = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );

    const { user } = renderWithProviders(
      <TransactionDialog
        txn={refund}
        categories={mockCategories}
        accounts={mockAccounts}
        accountLabelText="Salary Account"
        onClose={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: /Linked$/ }));
    await user.click(await screen.findByText("Remove link"));

    await waitFor(() => expect(deleted).toBe(true));
    expect(await screen.findByRole("button", { name: /Not linked/ })).toBeInTheDocument();
  });

  it("offers no refund picker on a spend", () => {
    let asked = false;
    server.use(
      http.get("*/api/v1/transactions/:id/refund-candidates", () => {
        asked = true;
        return HttpResponse.json([]);
      }),
    );

    renderWithProviders(
      <TransactionDialog
        txn={buildTransaction({ id: 52, amount_paise: -1000 })}
        categories={mockCategories}
        accounts={mockAccounts}
        accountLabelText="Salary Account"
        onClose={vi.fn()}
      />,
    );

    expect(screen.queryByText("Refund of")).not.toBeInTheDocument();
    expect(asked).toBe(false);
  });

  it("offers 'Paid your card bill elsewhere?' on an unpaired card payment and closes after linking", async () => {
    // ADR-0002 rule 11b: an uncategorised card payment commits as an unpaired transfer
    // (rule 7c), so the offer cannot hang on the row being income.
    const credit = buildTransaction({
      id: 31,
      account_id: 2,
      transaction_type: "transfer",
      amount_paise: 1500000,
      category_id: null,
      card_bill_link_offered: true,
    });
    server.use(
      http.get("*/api/v1/transactions/31/link-candidates", () =>
        HttpResponse.json([
          buildTransaction({ id: 9, account_id: 1, merchant_raw: "CARD PAYMENT", amount_paise: -1500000 }),
        ]),
      ),
    );
    const onClose = vi.fn();

    const { user } = renderWithProviders(
      <TransactionDialog
        txn={credit}
        categories={mockCategories}
        accounts={mockAccounts}
        accountLabelText="Amazon Pay CC"
        onClose={onClose}
      />,
    );

    expect(screen.getByText("Paid your card bill elsewhere?")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Pick the payment/ }));
    await user.click(await screen.findByText(/CARD PAYMENT/));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("renders the card-bill link only when the server offers it", async () => {
    // The placement is the server's `card_bill_link_offered` (ADR-0002 rule 11b); the
    // dialog never re-derives it from the account or the type.
    let asked = false;
    server.use(
      http.get("*/api/v1/transactions/:id/link-candidates", () => {
        asked = true;
        return HttpResponse.json([]);
      }),
    );

    const card = renderWithProviders(
      <TransactionDialog
        txn={buildTransaction({
          id: 32,
          account_id: 2,
          transaction_type: "income",
          amount_paise: 49900,
          card_bill_link_offered: false,
        })}
        categories={mockCategories}
        accounts={mockAccounts}
        accountLabelText="Amazon Pay CC"
        onClose={vi.fn()}
      />,
    );

    expect(screen.queryByText("Paid your card bill elsewhere?")).not.toBeInTheDocument();
    // Give a stray fetch time to land: the request is async, so a synchronous check is vacuous.
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(asked).toBe(false);
    card.unmount();

    renderWithProviders(
      <TransactionDialog
        txn={buildTransaction({
          id: 33,
          account_id: 1,
          transaction_type: "spend",
          amount_paise: -49900,
          card_bill_link_offered: true,
        })}
        categories={mockCategories}
        accounts={mockAccounts}
        accountLabelText="Salary Account"
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByText("Paid your card bill elsewhere?")).toBeInTheDocument();
  });

  it("reports a category correction to the board", async () => {
    const txn = buildTransaction({ id: 60, amount_paise: -50000, category_id: 1 });
    const onCategoryCorrected = vi.fn();
    const first = renderWithProviders(
      <TransactionDialog
        txn={txn}
        categories={mockCategories}
        accounts={mockAccounts}
        accountLabelText="Salary Account"
        onClose={vi.fn()}
        onCategoryCorrected={onCategoryCorrected}
      />,
    );

    await first.user.click(screen.getByRole("button", { name: /Food & Dining/ }));
    await first.user.click(await screen.findByText("Groceries"));
    await first.user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onCategoryCorrected).toHaveBeenCalledWith(60));
    first.unmount();

    // A merchant-only edit is not a correction.
    const second = vi.fn();
    const onClose = vi.fn();
    const { user } = renderWithProviders(
      <TransactionDialog
        txn={txn}
        categories={mockCategories}
        accounts={mockAccounts}
        accountLabelText="Salary Account"
        onClose={onClose}
        onCategoryCorrected={second}
      />,
    );
    await user.type(screen.getByDisplayValue(txn.merchant_raw ?? ""), "X");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(second).not.toHaveBeenCalled();
  });

  it("displays server error inline on 409 conflict", async () => {
    const txn = buildTransaction({ id: 99, amount_paise: -10000 });

    server.use(
      http.patch("*/api/v1/transactions/99", () => {
        return HttpResponse.json(
          { detail: "Transaction already exists (duplicate fingerprint)" },
          { status: 409 },
        );
      }),
    );

    const { user } = renderWithProviders(
      <TransactionDialog
        txn={txn}
        categories={mockCategories}
        accounts={mockAccounts}
        accountLabelText="Salary Account"
        onClose={vi.fn()}
      />,
    );

    // Edit merchant to trigger save
    const merchantInput = screen.getByDisplayValue("TEST MERCHANT");
    await user.clear(merchantInput);
    await user.type(merchantInput, "Duplicate Merchant");

    const saveButton = screen.getByRole("button", { name: "Save" });
    await user.click(saveButton);

    await waitFor(() => {
      expect(
        screen.getByText("Transaction already exists (duplicate fingerprint)"),
      ).toBeInTheDocument();
    });
  });

  it("offers 'Split with Friend' button and opens the split dialog", async () => {
    server.use(
      http.get("*/api/v1/friends/contacts", () => {
        return HttpResponse.json([]);
      }),
    );

    const txn = buildTransaction({
      id: 70,
      amount_paise: -250000,
      merchant_raw: "Swiggy Dineout",
    });

    const { user } = renderWithProviders(
      <TransactionDialog
        txn={txn}
        categories={mockCategories}
        accounts={mockAccounts}
        accountLabelText="Salary Account"
        onClose={vi.fn()}
      />,
    );

    const splitBtn = screen.getByRole("button", { name: /split with friend/i });
    expect(splitBtn).toBeInTheDocument();

    await user.click(splitBtn);
    expect(await screen.findByText("Split with Friend / Khata")).toBeInTheDocument();
  });
});
