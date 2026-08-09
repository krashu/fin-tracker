import type { TransactionType } from "@/lib/api/client";

/** The types a user may ASSIGN — on the manual-entry form (PRD §F2) or by editing
 * an existing row (ADR-0007 rule 1).
 *
 * `transfer` is absent deliberately, not by oversight. A transfer is a *pair* of
 * rows born together via `POST /transactions/transfer` with server-derived signs
 * (ADR-0002 exactly-two-pairing); minting one here would leave a lone leg, so the
 * backend rejects `transaction_type: "transfer"` on PATCH with a 422. Rows that
 * already ARE transfers still render — see `TRANSACTION_TYPE_LABELS`.
 */
export const EDITABLE_TRANSACTION_TYPES = ["spend", "refund", "income"] as const;

export type EditableTransactionType = (typeof EDITABLE_TRANSACTION_TYPES)[number];

/** Display label for every type, including the one that can't be chosen.
 *
 * Covers all four because an existing `transfer` row must still show "Transfer" in
 * a picker it cannot select — unpaired transfers are legal (they survive a delete
 * or an unlink) and are otherwise fully editable.
 */
export const TRANSACTION_TYPE_LABELS: Record<TransactionType, string> = {
  spend: "Spend",
  refund: "Refund",
  income: "Income",
  transfer: "Transfer",
};

/** Signed paise from a positive rupee magnitude, by type — the sign convention
 * PRD §F4a pins and the backend re-validates as a merged pair (ADR-0007 rule 4):
 * spend is negative, refund/income positive.
 *
 * `transfer` is not passed here: its legs' signs are server-derived. */
export function signedPaise(
  type: EditableTransactionType,
  magnitude: number,
): number {
  return type === "spend" ? -magnitude : magnitude;
}
