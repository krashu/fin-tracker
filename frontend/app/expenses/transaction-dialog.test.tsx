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

    // Open Direction dropdown (Spend / Refund / Income)
    const directionTrigger = screen.getByText("Spend");
    await user.click(directionTrigger);

    // Select Refund (ADR-0009: spend row with positive amount_paise)
    const refundOption = await screen.findByText("Refund");
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

  it("handles linked transfer pairs and unlinking (ADR-0002)", async () => {
    const pairedTxn = buildTransaction({
      id: 88,
      amount_paise: -1500000,
      transaction_type: "transfer",
      transfer_pair_id: 999,
    });

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
        onClose={vi.fn()}
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
});
