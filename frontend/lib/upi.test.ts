import { describe, expect, it } from "vitest";

import { buildUpiUri, formatWhatsAppBreakdown } from "./upi";

describe("UPI Utilities (lib/upi.ts)", () => {
  it("builds standard NPCI UPI URI with positive magnitude", () => {
    const uri = buildUpiUri({
      upiId: "alice@upi",
      name: "Alice Smith",
      amountPaise: 150000,
      notes: "Dinner split",
    });

    expect(uri).toBe(
      "upi://pay?pa=alice@upi&pn=Alice%20Smith&am=1500.00&cu=INR&tn=Dinner%20split",
    );

    // Negative magnitude is normalized to positive
    const negUri = buildUpiUri({
      upiId: "bob@upi",
      name: "Bob",
      amountPaise: -250000,
    });
    expect(negUri).toBe(
      "upi://pay?pa=bob@upi&pn=Bob&am=2500.00&cu=INR&tn=Settlement",
    );
  });

  it("formats WhatsApp breakdown message cleanly when friend owes user", () => {
    const mockContact = {
      id: 1,
      name: "Bob",
      phone: "+919876543210",
      upi_id: "bob@upi",
      notes: null,
      archived_at: null,
      created_at: "2026-03-01T00:00:00Z",
      balance_paise: 250000,
      total_gave_paise: 300000,
      total_got_paise: 50000,
      active_entries_count: 2,
    };

    const mockEntries = [
      {
        id: 1,
        contact_id: 1,
        transaction_id: null,
        date: "2026-03-01",
        amount_paise: 300000,
        direction: "you_gave" as const,
        is_settlement: false,
        notes: "Weekend trip hotel",
        created_at: "2026-03-01T00:00:00Z",
      },
      {
        id: 2,
        contact_id: 1,
        transaction_id: null,
        date: "2026-03-02",
        amount_paise: 50000,
        direction: "you_got" as const,
        is_settlement: false,
        notes: "Cab share",
        created_at: "2026-03-02T00:00:00Z",
      },
    ];

    const text = formatWhatsAppBreakdown({
      contact: mockContact,
      entries: mockEntries,
    });

    expect(text).toContain("Hi Bob, here is the breakdown of shared expenses:");
    expect(text).toContain("• 2026-03-01: Weekend trip hotel — +₹3,000.00");
    expect(text).toContain("• 2026-03-02: Cab share — -₹500.00");
    expect(text).toContain("Total Outstanding: ₹2,500.00 (owes you)");
    // Must NOT contain an inverted payment link telling Bob to pay himself
    expect(text).not.toContain("upi://pay");
  });

  it("formats WhatsApp breakdown with UPI ID when user owes friend", () => {
    const mockContact = {
      id: 2,
      name: "Charlie",
      phone: null,
      upi_id: "charlie@okaxis",
      notes: null,
      archived_at: null,
      created_at: "2026-03-01T00:00:00Z",
      balance_paise: -150000,
      total_gave_paise: 0,
      total_got_paise: 150000,
      active_entries_count: 1,
    };

    const text = formatWhatsAppBreakdown({
      contact: mockContact,
      entries: [
        {
          id: 3,
          contact_id: 2,
          transaction_id: null,
          date: "2026-03-05",
          amount_paise: 150000,
          direction: "you_got" as const,
          is_settlement: false,
          notes: "Movie tickets fronted",
          created_at: "2026-03-05T00:00:00Z",
        },
      ],
    });

    expect(text).toContain("Total Outstanding: ₹1,500.00 (you owe)");
    expect(text).toContain("UPI ID: charlie@okaxis");
  });
});
