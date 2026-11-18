import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { generateQrMatrix, QrCodeSvg } from "./qr";
import { buildUpiUri, formatWhatsAppBreakdown } from "./upi";

describe("QR Code & UPI Utilities (lib/qr.tsx & lib/upi.ts)", () => {
  it("builds standard NPCI UPI URI", () => {
    const uri = buildUpiUri({
      upiId: "alice@upi",
      name: "Alice Smith",
      amountPaise: 150000,
      notes: "Dinner split",
    });

    expect(uri).toBe(
      "upi://pay?pa=alice@upi&pn=Alice%20Smith&am=1500.00&cu=INR&tn=Dinner%20split",
    );
  });

  it("formats WhatsApp breakdown message cleanly", () => {
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
    expect(text).toContain("upi://pay?pa=bob@upi&pn=Bob&am=2500.00&cu=INR&tn=Settlement");
  });

  it("generates QR matrix with valid dimensions and finder patterns", () => {
    const upiUri = "upi://pay?pa=alice@upi&pn=Alice&am=100.00&cu=INR&tn=Settlement";
    const matrix = generateQrMatrix(upiUri);

    expect(matrix.length).toBeGreaterThanOrEqual(21);
    expect(matrix[0].length).toBe(matrix.length);

    // Top-left finder pattern check
    // Row 0 should have 7 consecutive dark modules at cols 0..6
    for (let c = 0; c < 7; c++) {
      expect(matrix[0][c]).toBe(true);
    }
    // Row 1 should have dark at 0 and 6, white at 1..5
    expect(matrix[1][0]).toBe(true);
    expect(matrix[1][6]).toBe(true);
    for (let c = 1; c <= 5; c++) {
      expect(matrix[1][c]).toBe(false);
    }
  });

  it("renders QrCodeSvg component with accessible title and svg elements", () => {
    render(
      <QrCodeSvg
        value="upi://pay?pa=test@upi&pn=Test&am=50.00&cu=INR&tn=Test"
        size={200}
        title="Payment QR"
      />,
    );

    const svg = screen.getByRole("img", { name: "Payment QR" });
    expect(svg).toBeInTheDocument();
    expect(svg).toHaveAttribute("width", "200");
    expect(svg).toHaveAttribute("height", "200");
  });
});
