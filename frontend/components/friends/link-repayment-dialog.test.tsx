import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";

import { LinkRepaymentDialog } from "./link-repayment-dialog";
import { server } from "@/tests/mocks/server";
import { renderWithProviders } from "@/tests/helpers/test-utils";
import type { TransactionRead } from "@/lib/api/client";

const mockCreditTxn: TransactionRead = {
  id: 201,
  account_id: 1,
  date: "2026-06-10",
  amount_paise: 200000, // ₹2,000 incoming credit
  transaction_type: "transfer",
  merchant_raw: "UPI / Repayment from Rahul",
  category_id: null,
  category_name: null,
  category_parent_name: null,
  transfer_pair_id: null,
  refund_of_id: null,
  card_bill_link_offered: false,
  labels: [],
};

describe("LinkRepaymentDialog (components/friends/link-repayment-dialog.tsx)", () => {
  it("renders incoming transaction context and links repayment with existing contact", async () => {
    let createdPayload: any = null;

    server.use(
      http.get("*/api/v1/friends/contacts", () => {
        return HttpResponse.json([
          {
            id: 1,
            name: "Rahul Sharma",
            phone: "9876543210",
            upi_id: "rahul@upi",
            notes: null,
            archived_at: null,
            created_at: "2026-01-01T00:00:00Z",
            balance_paise: 200000,
            total_gave_paise: 200000,
            total_got_paise: 0,
            active_entries_count: 1,
          },
        ]);
      }),
      http.post("*/api/v1/friends/entries", async ({ request }) => {
        createdPayload = await request.json();
        return HttpResponse.json({
          id: 601,
          contact_id: 1,
          transaction_id: 201,
          date: "2026-06-10",
          amount_paise: 200000,
          direction: "you_got",
          is_settlement: true,
          notes: "Dinner repayment",
          created_at: "2026-06-10T00:00:00Z",
        });
      }),
    );

    const onClose = vi.fn();
    const { user } = renderWithProviders(
      <LinkRepaymentDialog
        transaction={mockCreditTxn}
        open
        onClose={onClose}
      />,
      { initialBalanceHidden: false },
    );

    expect(screen.getByText("Link as Friend Repayment")).toBeInTheDocument();
    expect(screen.getByText("UPI / Repayment from Rahul")).toBeInTheDocument();
    expect(screen.getByText("+₹2,000.00")).toBeInTheDocument();

    // Select existing contact Rahul
    const pickerBtn = screen.getByRole("button", {
      name: /select friend \/ contact/i,
    });
    await user.click(pickerBtn);

    const rahulOption = await screen.findByRole("menuitem", {
      name: /rahul sharma/i,
    });
    await user.click(rahulOption);

    // Notes
    const notesInput = screen.getByPlaceholderText(/e\.g\. dinner repayment via upi/i);
    await user.type(notesInput, "Dinner repayment");

    // Submit
    const confirmBtn = screen.getByRole("button", { name: /confirm repayment/i });
    expect(confirmBtn).toBeEnabled();
    await user.click(confirmBtn);

    await waitFor(() => {
      expect(createdPayload).toEqual({
        contact_id: 1,
        transaction_id: 201,
        date: "2026-06-10",
        amount_paise: 200000,
        direction: "you_got",
        is_settlement: true,
        notes: "Dinner repayment",
      });
    });

    expect(onClose).toHaveBeenCalled();
  });

  it("enforces allocation guard validation against incoming credit amount", async () => {
    server.use(
      http.get("*/api/v1/friends/contacts", () => {
        return HttpResponse.json([
          {
            id: 1,
            name: "Rahul Sharma",
            phone: null,
            upi_id: null,
            notes: null,
            archived_at: null,
            created_at: "2026-01-01T00:00:00Z",
            balance_paise: 500000,
            total_gave_paise: 500000,
            total_got_paise: 0,
            active_entries_count: 1,
          },
        ]);
      }),
    );

    const { user } = renderWithProviders(
      <LinkRepaymentDialog
        transaction={mockCreditTxn}
        open
        onClose={() => {}}
      />,
      { initialBalanceHidden: false },
    );

    // Select Rahul
    const pickerBtn = screen.getByRole("button", {
      name: /select friend \/ contact/i,
    });
    await user.click(pickerBtn);
    const rahulOption = await screen.findByRole("menuitem", {
      name: /rahul sharma/i,
    });
    await user.click(rahulOption);

    // Change amount to exceed transaction total (2,000 -> 3,000)
    const amountInput = screen.getByLabelText(/repayment amount \(₹\)/i);
    await user.clear(amountInput);
    await user.type(amountInput, "3000.00");

    const confirmBtn = screen.getByRole("button", { name: /confirm repayment/i });
    expect(confirmBtn).toBeDisabled();

    // Change to zero
    await user.clear(amountInput);
    await user.type(amountInput, "0");
    expect(confirmBtn).toBeDisabled();
  });

  it("allows quick-adding a new friend inline during repayment linking", async () => {
    let createdContact: any = null;
    let createdEntry: any = null;

    server.use(
      http.get("*/api/v1/friends/contacts", () => HttpResponse.json([])),
      http.post("*/api/v1/friends/contacts", async ({ request }) => {
        createdContact = await request.json();
        return HttpResponse.json({
          id: 99,
          name: createdContact.name,
          phone: createdContact.phone,
          upi_id: createdContact.upi_id,
          notes: null,
          archived_at: null,
          created_at: "2026-06-10T00:00:00Z",
        });
      }),
      http.post("*/api/v1/friends/entries", async ({ request }) => {
        createdEntry = await request.json();
        return HttpResponse.json({
          id: 602,
          contact_id: 99,
          transaction_id: 201,
          date: "2026-06-10",
          amount_paise: 200000,
          direction: "you_got",
          is_settlement: true,
          notes: null,
          created_at: "2026-06-10T00:00:00Z",
        });
      }),
    );

    const onClose = vi.fn();
    const { user } = renderWithProviders(
      <LinkRepaymentDialog
        transaction={mockCreditTxn}
        open
        onClose={onClose}
      />,
    );

    // Select Add New Friend
    const pickerBtn = screen.getByRole("button", {
      name: /select friend \/ contact/i,
    });
    await user.click(pickerBtn);

    const addOption = await screen.findByRole("menuitem", {
      name: /add new friend/i,
    });
    await user.click(addOption);

    // Fill new friend fields
    const nameInput = screen.getByPlaceholderText(/friend's name \*/i);
    await user.type(nameInput, "Sneha Kapoor");

    const upiInput = screen.getByPlaceholderText(/upi id \(optional\)/i);
    await user.type(upiInput, "sneha@okhdfcbank");

    const confirmBtn = screen.getByRole("button", { name: /confirm repayment/i });
    expect(confirmBtn).toBeEnabled();
    await user.click(confirmBtn);

    await waitFor(() => {
      expect(createdContact).toEqual({
        name: "Sneha Kapoor",
        phone: null,
        upi_id: "sneha@okhdfcbank",
      });
      expect(createdEntry).toEqual({
        contact_id: 99,
        transaction_id: 201,
        date: "2026-06-10",
        amount_paise: 200000,
        direction: "you_got",
        is_settlement: true,
        notes: null,
      });
    });

    expect(onClose).toHaveBeenCalled();
  });
});
