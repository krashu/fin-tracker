import type { TransactionType } from "@/lib/api/client";

/** The types a user may ASSIGN — on the manual-entry form (PRD §F2) or by
 * editing an existing row (ADR-0007 rule 1).
 *
 * `transfer` is absent deliberately, not by oversight. A transfer is a *pair* of
 * rows born together via `POST /transactions/transfer` with server-derived signs
 * (ADR-0002 exactly-two-pairing); minting one here would leave a lone leg, so the
 * backend rejects `transaction_type: "transfer"` on PATCH with a 422. Rows that
 * already ARE transfers still render — see `TRANSACTION_TYPE_LABELS`.
 *
 * There is no `"refund"` here (ADR-0009): a refund is a `spend` row carrying a
 * positive `amount_paise`, not a stored type. The manual-entry/edit forms still
 * offer a three-way Spend/Refund/Income choice — see `EntryDirection` below.
 */
export const EDITABLE_TRANSACTION_TYPES = ["spend", "income"] as const;

export type EditableTransactionType = (typeof EDITABLE_TRANSACTION_TYPES)[number];

/** Display label for every type, including the one that can't be chosen.
 *
 * Covers all three because an existing `transfer` row must still show "Transfer" in
 * a picker it cannot select — unpaired transfers are legal (they survive a delete
 * or an unlink) and are otherwise fully editable.
 */
export const TRANSACTION_TYPE_LABELS: Record<TransactionType, string> = {
  spend: "Spend",
  income: "Income",
  transfer: "Transfer",
};

/** A refund is not its own type (ADR-0009) — it's a `spend` row carrying a
 * *positive* `amount_paise`, derived at read time and never stored. This is
 * the single predicate every consumer should use rather than re-deriving the
 * `transaction_type === "spend" && amount_paise > 0` check inline. */
export function isRefund(t: {
  transaction_type: TransactionType;
  amount_paise: number;
}): boolean {
  return t.transaction_type === "spend" && t.amount_paise > 0;
}

/** UI-level entry direction for the manual-entry and edit forms — the
 * three-way choice PRD §F2 offers even though the backend stores only two
 * editable types (ADR-0009). Maps to `{type, sign}`:
 *  - `spend`  → (`spend`, negative) — an outflow
 *  - `refund` → (`spend`, positive) — nets against spend in the same category
 *  - `income` → (`income`, positive)
 *
 * `transfer` has no direction — its legs' signs are server-derived, and
 * `EDITABLE_TRANSACTION_TYPES` already excludes it as a NEW selection.
 */
export const ENTRY_DIRECTIONS = ["spend", "refund", "income"] as const;

export type EntryDirection = (typeof ENTRY_DIRECTIONS)[number];

export const ENTRY_DIRECTION_LABELS: Record<EntryDirection, string> = {
  spend: "Spend",
  refund: "Refund",
  income: "Income",
};

const ENTRY_DIRECTION_TYPE: Record<EntryDirection, EditableTransactionType> = {
  spend: "spend",
  refund: "spend",
  income: "income",
};

/** The stored `transaction_type` for a given entry direction — `refund` folds
 * into `spend` (ADR-0009). */
export function directionToType(direction: EntryDirection): EditableTransactionType {
  return ENTRY_DIRECTION_TYPE[direction];
}

/** Recover the entry direction an existing row was last saved as — the inverse
 * of `directionToType`/`signedPaise`, used to seed the edit dialog from a
 * stored row. Not meaningful for a `transfer` leg; callers keep transfers on
 * their own display/sign path (transaction-dialog.tsx) rather than calling
 * this. */
export function directionForTransaction(t: {
  transaction_type: TransactionType;
  amount_paise: number;
}): EntryDirection {
  if (t.transaction_type === "income") return "income";
  return isRefund(t) ? "refund" : "spend";
}

/** Signed paise from a positive rupee magnitude, by entry direction — the
 * sign convention PRD §F4a pins and the backend re-validates via `sign_error`
 * (ADR-0007 rule 4, amended by ADR-0009): spend is negative, refund/income
 * positive.
 *
 * `transfer` is not passed here: its legs' signs are server-derived. */
export function signedPaise(direction: EntryDirection, magnitude: number): number {
  return direction === "spend" ? -magnitude : magnitude;
}
