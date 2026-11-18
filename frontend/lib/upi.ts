/**
 * UPI URI & WhatsApp message formatting for Peer Ledger (PRD §F3b, ADR-0017).
 */
import { formatINR } from "@/lib/format";
import type { PeerContactWithBalance, PeerEntry } from "@/lib/api/client";

export interface BuildUpiUriOptions {
  upiId: string;
  name: string;
  amountPaise: number;
  notes?: string;
}

/**
 * Construct standard NPCI UPI URI:
 * upi://pay?pa={upi_id}&pn={encoded_name}&am={balance_in_rupees}&cu=INR&tn={notes}
 */
export function buildUpiUri({
  upiId,
  name,
  amountPaise,
  notes = "Settlement",
}: BuildUpiUriOptions): string {
  const balanceInRupees = (Math.abs(amountPaise) / 100).toFixed(2);
  const encodedName = encodeURIComponent(name);
  const encodedNotes = encodeURIComponent(notes);
  return `upi://pay?pa=${upiId}&pn=${encodedName}&am=${balanceInRupees}&cu=INR&tn=${encodedNotes}`;
}

/**
 * Format unsettled entries into a clean, human-readable breakdown for sharing on WhatsApp.
 */
export function formatWhatsAppBreakdown({
  contact,
  entries,
}: {
  contact: PeerContactWithBalance;
  entries: PeerEntry[];
}): string {
  const unsettled = entries.filter((e) => !e.is_settlement);
  const lines: string[] = [];

  lines.push(`Hi ${contact.name}, here is the breakdown of shared expenses:`);
  lines.push("");

  if (unsettled.length === 0) {
    lines.push("No unsettled entries recorded.");
  } else {
    for (const e of unsettled) {
      const sign = e.direction === "you_gave" ? "+" : "-";
      const desc =
        e.notes || (e.direction === "you_gave" ? "You gave" : "You got");
      lines.push(`• ${e.date}: ${desc} — ${sign}${formatINR(e.amount_paise)}`);
    }
  }

  lines.push("");
  const balanceINR = formatINR(Math.abs(contact.balance_paise));
  if (contact.balance_paise > 0) {
    lines.push(`Total Outstanding: ${balanceINR} (owes you)`);
  } else if (contact.balance_paise < 0) {
    lines.push(`Total Outstanding: ${balanceINR} (you owe)`);
    if (contact.upi_id) {
      lines.push(`UPI ID: ${contact.upi_id}`);
    }
  } else {
    lines.push("Total Outstanding: ₹0 (All settled up!)");
  }

  return lines.join("\n");
}
