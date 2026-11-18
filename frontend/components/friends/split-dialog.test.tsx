import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";

import { SplitDialog } from "./split-dialog";
import { server } from "@/tests/mocks/server";
import { renderWithProviders } from "@/tests/helpers/test-utils";
import type { TransactionRead } from "@/lib/api/client";

const mockTxn: TransactionRead = {
  id: 101,
  account_id: 1,
  date: "2026-05-15",
  amount_paise: -300000, // ₹3,000 spend
  transaction_type: "spend",
  merchant_raw: "Amazon India",
  category_id: 2,
  category_name: "Shopping",
  category_parent_name: null,
  transfer_pair_id: null,
  refund_of_id: null,
  card_bill_link_offered: false,
  labels: [],
};

describe("SplitDialog (components/friends/split-dialog.tsx)", () => {
  it("renders transaction context and splits with existing contact", async () => {
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
            balance_paise: 0,
            total_gave_paise: 0,
            total_got_paise: 0,
            active_entries_count: 0,
          },
        ]);
      }),
      http.post("*/api/v1/friends/entries", async ({ request }) => {
        createdPayload = await request.json();
        return HttpResponse.json({
          id: 501,
          contact_id: 1,
          transaction_id: 101,
          date: "2026-05-15",
          amount_paise: 150000,
          direction: "you_gave",
          is_settlement: false,
          notes: "50% split",
          created_at: "2026-05-15T00:00:00Z",
        });
      }),
    );

    const onClose = vi.fn();
    const { user } = renderWithProviders(
      <SplitDialog transaction={mockTxn} open onClose={onClose} />,
      { initialBalanceHidden: false },
    );

    expect(screen.getByText("Split with Friend / Khata")).toBeInTheDocument();
    expect(screen.getByText("Amazon India")).toBeInTheDocument();

    // Select existing contact
    const contactPicker = screen.getByRole("button", {
      name: /friend \/ contact/i,
    });
    await user.click(contactPicker);
    await user.click(await screen.findByText("Rahul Sharma"));

    // Set allocated amount to 1500.00
    const amountInput = screen.getByLabelText(/allocated amount/i);
    await user.clear(amountInput);
    await user.type(amountInput, "1500.00");

    // Enter notes
    const notesInput = screen.getByLabelText(/notes/i);
    await user.type(notesInput, "50% split");

    // Click Record Split
    await user.click(screen.getByRole("button", { name: "Record Split" }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(createdPayload).toMatchObject({
      contact_id: 1,
      transaction_id: 101,
      date: "2026-05-15",
      amount_paise: 150000,
      direction: "you_gave",
      notes: "50% split",
    });
  });

  it("enforces allocation guard validation against transaction amount", async () => {
    server.use(
      http.get("*/api/v1/friends/contacts", () => {
        return HttpResponse.json([
          {
            id: 2,
            name: "Pooja",
            phone: null,
            upi_id: null,
            notes: null,
            archived_at: null,
            created_at: "2026-01-01T00:00:00Z",
            balance_paise: 0,
            total_gave_paise: 0,
            total_got_paise: 0,
            active_entries_count: 0,
          },
        ]);
      }),
    );

    const { user } = renderWithProviders(
      <SplitDialog transaction={mockTxn} open onClose={() => {}} />,
      { initialBalanceHidden: false },
    );

    // Select contact
    const contactPicker = screen.getByRole("button", {
      name: /friend \/ contact/i,
    });
    await user.click(contactPicker);
    await user.click(await screen.findByText("Pooja"));

    // Enter amount greater than transaction total (₹4,000 > ₹3,000)
    const amountInput = screen.getByLabelText(/allocated amount/i);
    await user.clear(amountInput);
    await user.type(amountInput, "4000.00");

    await user.click(screen.getByRole("button", { name: "Record Split" }));

    expect(
      await screen.findByText(/cannot exceed transaction total/i),
    ).toBeInTheDocument();
  });

  it("calculates upfront discount / cashback offset and passes original and discount paise", async () => {
    let createdPayload: any = null;

    server.use(
      http.get("*/api/v1/friends/contacts", () => {
        return HttpResponse.json([
          {
            id: 3,
            name: "Ananya",
            phone: null,
            upi_id: null,
            notes: null,
            archived_at: null,
            created_at: "2026-01-01T00:00:00Z",
            balance_paise: 0,
            total_gave_paise: 0,
            total_got_paise: 0,
            active_entries_count: 0,
          },
        ]);
      }),
      http.post("*/api/v1/friends/entries", async ({ request }) => {
        createdPayload = await request.json();
        return HttpResponse.json({
          id: 502,
          contact_id: 3,
          transaction_id: 101,
          date: "2026-05-15",
          amount_paise: 285000,
          direction: "you_gave",
          is_settlement: false,
          notes: "Amazon 5% discount applied",
          created_at: "2026-05-15T00:00:00Z",
        });
      }),
    );

    const onClose = vi.fn();
    const { user } = renderWithProviders(
      <SplitDialog transaction={mockTxn} open onClose={onClose} />,
      { initialBalanceHidden: false },
    );

    // Select contact
    await user.click(
      screen.getByRole("button", { name: /friend \/ contact/i }),
    );
    await user.click(await screen.findByText("Ananya"));

    // Check discount checkbox
    await user.click(
      screen.getByLabelText("Apply card cashback / discount"),
    );

    // Original is 3000.00, set discount to 150.00 (5% cashback)
    const discountInput = screen.getByLabelText(/discount deduction/i);
    await user.clear(discountInput);
    await user.type(discountInput, "150.00");

    // Allocated amount should now be 2850.00
    const amountInput = screen.getByLabelText(/allocated amount/i);
    expect(amountInput).toHaveValue(2850);

    const notesInput = screen.getByLabelText(/notes/i);
    await user.type(notesInput, "Amazon 5% discount applied");

    await user.click(screen.getByRole("button", { name: "Record Split" }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(createdPayload).toMatchObject({
      contact_id: 3,
      transaction_id: 101,
      amount_paise: 285000,
      original_paise: 300000,
      discount_paise: 15000,
      direction: "you_gave",
    });
  });

  it("allows quick-adding a new contact inline during split", async () => {
    let createdContactPayload: any = null;
    let createdEntryPayload: any = null;

    server.use(
      http.get("*/api/v1/friends/contacts", () => {
        return HttpResponse.json([]);
      }),
      http.post("*/api/v1/friends/contacts", async ({ request }) => {
        createdContactPayload = await request.json();
        return HttpResponse.json({
          id: 99,
          name: "Siddharth",
          phone: "9876500000",
          upi_id: "sid@upi",
          notes: null,
          archived_at: null,
          created_at: "2026-01-01T00:00:00Z",
        });
      }),
      http.post("*/api/v1/friends/entries", async ({ request }) => {
        createdEntryPayload = await request.json();
        return HttpResponse.json({
          id: 601,
          contact_id: 99,
          transaction_id: 101,
          date: "2026-05-15",
          amount_paise: 300000,
          direction: "you_gave",
          is_settlement: false,
          notes: null,
          created_at: "2026-05-15T00:00:00Z",
        });
      }),
    );

    const onClose = vi.fn();
    const { user } = renderWithProviders(
      <SplitDialog transaction={mockTxn} open onClose={onClose} />,
      { initialBalanceHidden: false },
    );

    // Open contact picker and click Add new contact
    await user.click(
      screen.getByRole("button", { name: /friend \/ contact/i }),
    );
    await user.click(await screen.findByText("Add new contact"));

    // Fill inline contact details
    await user.type(screen.getByPlaceholderText("Full Name *"), "Siddharth");
    await user.type(
      screen.getByPlaceholderText(/phone/i),
      "9876500000",
    );
    await user.type(screen.getByPlaceholderText(/upi id/i), "sid@upi");

    await user.click(screen.getByRole("button", { name: "Record Split" }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(createdContactPayload).toMatchObject({
      name: "Siddharth",
      phone: "9876500000",
      upi_id: "sid@upi",
    });
    expect(createdEntryPayload).toMatchObject({
      contact_id: 99,
      transaction_id: 101,
      amount_paise: 300000,
      direction: "you_gave",
    });
  });
});
