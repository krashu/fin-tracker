import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";

import { FriendsDashboard } from "./friends-dashboard";
import { server } from "@/tests/mocks/server";
import { renderWithProviders } from "@/tests/helpers/test-utils";

const mockSummary = {
  total_receivable_paise: 250000, // ₹2,500
  total_payable_paise: 100000, // ₹1,000
  net_balance_paise: 150000, // ₹1,500
  active_contacts_count: 2,
};

const mockContacts = [
  {
    id: 1,
    name: "Aman Verma",
    phone: "9876543210",
    upi_id: "aman@upi",
    notes: "Flatmate",
    archived_at: null,
    created_at: "2026-01-01T00:00:00Z",
    balance_paise: 250000,
    total_gave_paise: 250000,
    total_got_paise: 0,
    active_entries_count: 2,
  },
  {
    id: 2,
    name: "Neha Gupta",
    phone: null,
    upi_id: "neha@okaxis",
    notes: null,
    archived_at: null,
    created_at: "2026-01-02T00:00:00Z",
    balance_paise: -100000,
    total_gave_paise: 0,
    total_got_paise: 100000,
    active_entries_count: 1,
  },
];

const mockAmanEntries = [
  {
    id: 10,
    contact_id: 1,
    transaction_id: null,
    date: "2026-05-10",
    amount_paise: 150000,
    direction: "you_gave",
    is_settlement: false,
    notes: "Groceries split",
    created_at: "2026-05-10T00:00:00Z",
  },
  {
    id: 11,
    contact_id: 1,
    transaction_id: 55,
    date: "2026-05-12",
    amount_paise: 100000,
    direction: "you_gave",
    is_settlement: false,
    notes: "Dinner bill",
    created_at: "2026-05-12T00:00:00Z",
  },
];

describe("FriendsDashboard (app/friends/friends-dashboard.tsx)", () => {
  it("renders summary metrics masked by default and unmasks when visible", async () => {
    server.use(
      http.get("*/api/v1/friends/summary", () => {
        return HttpResponse.json(mockSummary);
      }),
      http.get("*/api/v1/friends/contacts", () => {
        return HttpResponse.json(mockContacts);
      }),
    );

    // Render with initialBalanceHidden = true (the app default)
    const { unmount } = renderWithProviders(<FriendsDashboard />, {
      initialBalanceHidden: true,
    });

    expect(screen.getByText("You will get")).toBeInTheDocument();
    expect(screen.getByText("You will give")).toBeInTheDocument();
    expect(screen.getByText("Net Balance")).toBeInTheDocument();

    // Sensitive values are masked with ••••
    const maskedAmounts = screen.getAllByText("••••");
    expect(maskedAmounts.length).toBeGreaterThanOrEqual(3);

    unmount();

    // Render with initialBalanceHidden = false to test actual unmasked values
    renderWithProviders(<FriendsDashboard />, {
      initialBalanceHidden: false,
    });

    await waitFor(() => {
      expect(screen.getAllByText("₹2,500.00")).toHaveLength(2);
    });
    expect(screen.getAllByText("₹1,000.00")).toHaveLength(2);
    expect(screen.getByText("₹1,500.00")).toBeInTheDocument();
  });

  it("lists contacts, filters by search query, and toggles archived", async () => {
    let requestedIncludeArchived = false;

    server.use(
      http.get("*/api/v1/friends/summary", () => HttpResponse.json(mockSummary)),
      http.get("*/api/v1/friends/contacts", ({ request }) => {
        const url = new URL(request.url);
        requestedIncludeArchived = url.searchParams.get("include_archived") === "true";
        return HttpResponse.json(mockContacts);
      }),
    );

    const { user } = renderWithProviders(<FriendsDashboard />, {
      initialBalanceHidden: false,
    });

    expect(await screen.findByText("Aman Verma")).toBeInTheDocument();
    expect(screen.getByText("Neha Gupta")).toBeInTheDocument();

    // Search filter: type "Aman"
    const searchInput = screen.getByPlaceholderText(/search contacts/i);
    await user.type(searchInput, "Aman");

    expect(screen.getByText("Aman Verma")).toBeInTheDocument();
    expect(screen.queryByText("Neha Gupta")).not.toBeInTheDocument();

    await user.clear(searchInput);
    expect(screen.getByText("Neha Gupta")).toBeInTheDocument();

    // Toggle show archived
    const archivedToggle = screen.getByLabelText(/show archived/i);
    await user.click(archivedToggle);
    expect(requestedIncludeArchived).toBe(true);
  });

  it("opens add contact modal, creates a new contact, and invalidates cache", async () => {
    let createdPayload: any = null;

    server.use(
      http.get("*/api/v1/friends/summary", () => HttpResponse.json(mockSummary)),
      http.get("*/api/v1/friends/contacts", () => HttpResponse.json(mockContacts)),
      http.post("*/api/v1/friends/contacts", async ({ request }) => {
        createdPayload = await request.json();
        return HttpResponse.json({
          id: 3,
          name: "Karan Patel",
          phone: "9876511111",
          upi_id: "karan@upi",
          notes: "Gym buddy",
          archived_at: null,
          created_at: "2026-05-15T00:00:00Z",
        });
      }),
    );

    const { user } = renderWithProviders(<FriendsDashboard />, {
      initialBalanceHidden: false,
    });

    await user.click(screen.getByRole("button", { name: /add friend \/ contact/i }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Add Friend / Contact")).toBeInTheDocument();

    await user.type(within(dialog).getByLabelText(/name \*/i), "Karan Patel");
    await user.type(within(dialog).getByLabelText(/phone number/i), "9876511111");
    await user.type(within(dialog).getByLabelText(/upi id/i), "karan@upi");
    await user.type(within(dialog).getByLabelText(/notes/i), "Gym buddy");

    await user.click(within(dialog).getByRole("button", { name: "Add Contact" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(createdPayload).toEqual({
      name: "Karan Patel",
      phone: "9876511111",
      upi_id: "karan@upi",
      notes: "Gym buddy",
    });
  });

  it("opens contact ledger dialog, views chronological entries, and settles up balance", async () => {
    let settledContactId = 0;
    let settlePayload: any = null;

    server.use(
      http.get("*/api/v1/friends/summary", () => HttpResponse.json(mockSummary)),
      http.get("*/api/v1/friends/contacts", () => HttpResponse.json(mockContacts)),
      http.get("*/api/v1/friends/contacts/1", () => HttpResponse.json(mockContacts[0])),
      http.get("*/api/v1/friends/contacts/1/entries", () => HttpResponse.json(mockAmanEntries)),
      http.post("*/api/v1/friends/contacts/:id/settle", async ({ params, request }) => {
        settledContactId = Number(params.id);
        settlePayload = await request.json();
        return HttpResponse.json({
          id: 99,
          contact_id: 1,
          transaction_id: null,
          date: "2026-05-15",
          amount_paise: 250000,
          direction: "you_got",
          is_settlement: true,
          notes: "Full settlement",
          created_at: "2026-05-15T00:00:00Z",
        });
      }),
    );

    const { user } = renderWithProviders(<FriendsDashboard />, {
      initialBalanceHidden: false,
    });

    // Click on Aman Verma card to open ledger
    await user.click(await screen.findByText("Aman Verma"));

    const ledgerDialog = await screen.findByRole("dialog");
    expect(within(ledgerDialog).getByText("Groceries split")).toBeInTheDocument();
    expect(within(ledgerDialog).getByText("Dinner bill")).toBeInTheDocument();

    // Click Settle Up
    const settleBtn = within(ledgerDialog).getByRole("button", { name: /settle up/i });
    await user.click(settleBtn);

    // In settle confirmation modal
    const settleModal = await screen.findByText(/settle balance with aman verma/i);
    expect(settleModal).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Confirm Settle Up" }));

    await waitFor(() => expect(settledContactId).toBe(1));
    expect(settlePayload).toMatchObject({
      notes: "Full settlement",
    });
  });

  it("deletes a ledger entry with confirmation", async () => {
    let deletedEntryId = 0;

    server.use(
      http.get("*/api/v1/friends/summary", () => HttpResponse.json(mockSummary)),
      http.get("*/api/v1/friends/contacts", () => HttpResponse.json(mockContacts)),
      http.get("*/api/v1/friends/contacts/1", () => HttpResponse.json(mockContacts[0])),
      http.get("*/api/v1/friends/contacts/1/entries", () => HttpResponse.json(mockAmanEntries)),
      http.delete("*/api/v1/friends/entries/:id", ({ params }) => {
        deletedEntryId = Number(params.id);
        return new HttpResponse(null, { status: 204 });
      }),
    );

    const { user } = renderWithProviders(<FriendsDashboard />, {
      initialBalanceHidden: false,
    });

    await user.click(await screen.findByText("Aman Verma"));
    await screen.findByText("Groceries split");

    const deleteBtns = screen.getAllByRole("button", { name: /delete entry/i });
    await user.click(deleteBtns[0]);

    // Confirmation dialog
    expect(await screen.findByText("Delete Ledger Entry?")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Delete Entry" }));

    await waitFor(() => expect(deletedEntryId).toBe(10));
  });

  it("prompts warning when archiving a contact with a non-zero balance and confirms archive", async () => {
    let archivedContactId = 0;
    let confirmedWithBalance = false;

    server.use(
      http.get("*/api/v1/friends/summary", () => HttpResponse.json(mockSummary)),
      http.get("*/api/v1/friends/contacts", () => HttpResponse.json(mockContacts)),
      http.get("*/api/v1/friends/contacts/1", () => HttpResponse.json(mockContacts[0])),
      http.get("*/api/v1/friends/contacts/1/entries", () => HttpResponse.json(mockAmanEntries)),
      http.delete("*/api/v1/friends/contacts/:id", ({ params, request }) => {
        archivedContactId = Number(params.id);
        const url = new URL(request.url);
        confirmedWithBalance = url.searchParams.get("confirm_with_balance") === "true";
        return HttpResponse.json({
          ...mockContacts[0],
          archived_at: "2026-05-15T00:00:00Z",
        });
      }),
    );

    const { user } = renderWithProviders(<FriendsDashboard />, {
      initialBalanceHidden: false,
    });

    await user.click(await screen.findByText("Aman Verma"));

    const archiveBtn = screen.getByRole("button", { name: /archive contact/i });
    await user.click(archiveBtn);

    // Warning alert regarding outstanding balance
    expect(await screen.findByText(/outstanding balance of/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Confirm Archive" }));

    await waitFor(() => expect(archivedContactId).toBe(1));
    expect(confirmedWithBalance).toBe(true);
  });

  it("renders UPI payment link and dynamic QR code on ledger dialog, copying UPI and WhatsApp breakdown", async () => {
    const writeTextSpy = vi
      .spyOn(navigator.clipboard, "writeText")
      .mockResolvedValue(undefined);

    server.use(
      http.get("*/api/v1/friends/summary", () => HttpResponse.json(mockSummary)),
      http.get("*/api/v1/friends/contacts", () => HttpResponse.json(mockContacts)),
      http.get("*/api/v1/friends/contacts/1", () => HttpResponse.json(mockContacts[0])),
      http.get("*/api/v1/friends/contacts/1/entries", () => HttpResponse.json(mockAmanEntries)),
    );

    const { user } = renderWithProviders(<FriendsDashboard />, {
      initialBalanceHidden: false,
    });

    // On ContactCard for Aman (balance_paise: 250000, upi_id: "aman@upi")
    const cardCopyUpiBtns = await screen.findAllByRole("button", {
      name: /copy upi/i,
    });
    const cardCopyUpiBtn = cardCopyUpiBtns[0];
    expect(cardCopyUpiBtn).toBeInTheDocument();

    // Click card Copy UPI -> copies UPI ID
    fireEvent.click(cardCopyUpiBtn);
    await waitFor(() => {
      expect(writeTextSpy).toHaveBeenCalledWith("aman@upi");
    });

    // Open Ledger Dialog
    await user.click(await screen.findByText("Aman Verma"));

    // Copy UPI ID inside dialog
    const copyUpiBtn = screen.getByRole("button", { name: /copy upi id/i });
    await user.click(copyUpiBtn);
    expect(writeTextSpy).toHaveBeenCalledWith("aman@upi");

    // Copy WhatsApp breakdown inside dialog
    const copyWhatsAppBtn = screen.getByRole("button", {
      name: /copy breakdown for whatsapp/i,
    });
    await user.click(copyWhatsAppBtn);
    expect(writeTextSpy).toHaveBeenCalledWith(
      expect.stringContaining("Hi Aman Verma, here is the breakdown of shared expenses:"),
    );
  });

  it("exports contact ledger statement via Export Statement button", async () => {
    let exportRequested = false;

    server.use(
      http.get("*/api/v1/friends/summary", () => HttpResponse.json(mockSummary)),
      http.get("*/api/v1/friends/contacts", () => HttpResponse.json(mockContacts)),
      http.get("*/api/v1/friends/contacts/1", () => HttpResponse.json(mockContacts[0])),
      http.get("*/api/v1/friends/contacts/1/entries", () => HttpResponse.json(mockAmanEntries)),
      http.get("*/api/v1/friends/contacts/1/export", () => {
        exportRequested = true;
        return new HttpResponse(
          "Date,Description,Direction,Amount (INR),Settlement Status,Running Balance (INR)\n2026-05-10,Groceries split,you_gave,1500.00,Regular,1500.00",
          {
            headers: {
              "content-type": "text/csv; charset=utf-8",
              "content-disposition": 'attachment; filename="peer_contact_1_statement.csv"',
            },
          },
        );
      }),
    );

    // Mock createObjectURL & revokeObjectURL
    const createObjectURLMock = vi.fn().mockReturnValue("blob:mock-url");
    const revokeObjectURLMock = vi.fn();
    window.URL.createObjectURL = createObjectURLMock;
    window.URL.revokeObjectURL = revokeObjectURLMock;

    const { user } = renderWithProviders(<FriendsDashboard />, {
      initialBalanceHidden: false,
    });

    await user.click(await screen.findByText("Aman Verma"));

    const exportBtn = await screen.findByRole("button", {
      name: /export statement/i,
    });
    expect(exportBtn).toBeInTheDocument();

    await user.click(exportBtn);

    await waitFor(() => {
      expect(exportRequested).toBe(true);
      expect(createObjectURLMock).toHaveBeenCalled();
    });
  });
});
