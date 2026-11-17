import type { TransactionType } from "@/lib/api/client";

/** The types the manual-entry form offers (PRD §F2) and an edit may assign (ADR-0007 rule 1).
 *
 * `transfer` is absent from this list on purpose: the add form stays spend/income. An
 * existing unpaired row may be retyped to `transfer` in the edit dialog (ADR-0002 rule 7d),
 * which offers it beside the four entry directions. Rows that already ARE transfers still
 * render — see `TRANSACTION_TYPE_LABELS`.
 *
 * There is no `"refund"` here (ADR-0009): a refund is a `spend` row carrying a
 * positive `amount_paise`, not a stored type. The manual-entry/edit forms still
 * offer a four-way Spend/Refund/Income/Income-reversal choice — see
 * `EntryDirection` below.
 */
export const EDITABLE_TRANSACTION_TYPES = ["spend", "income"] as const;

export type EditableTransactionType = (typeof EDITABLE_TRANSACTION_TYPES)[number];

/** Display label for every type, including the one the add form does not offer.
 *
 * Covers all three because an existing `transfer` row must still show "Transfer" in the
 * edit dialog's picker — unpaired transfers are legal (they survive a delete or an unlink,
 * an unlinked card-bill debit commits as one, ADR-0002 rule 7b, and a user may write one,
 * rule 7d) and are otherwise fully editable.
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
 * four-way choice PRD §F2 offers even though the backend stores only two
 * editable types (ADR-0009). Maps to `{type, sign}`:
 *  - `spend`  → (`spend`, negative) — an outflow
 *  - `refund` → (`spend`, positive) — nets against spend in the same category.
 *    Labelled "Refund / cashback": a purchase cashback is a rebate recorded
 *    through this direction too (ADR-0013 rule 1)
 *  - `income` → (`income`, positive)
 *  - `income_reversal` → (`income`, negative) — nets against income in the
 *    same category (ADR-0009 rule 12)
 *
 * `transfer` has no direction — its legs' signs are server-derived. Only the edit dialog
 * offers it (ADR-0002 rule 7d), as its own item that keeps the row's stored sign.
 */
export const ENTRY_DIRECTIONS = ["spend", "refund", "income", "income_reversal"] as const;

export type EntryDirection = (typeof ENTRY_DIRECTIONS)[number];

export const ENTRY_DIRECTION_LABELS: Record<EntryDirection, string> = {
  spend: "Spend",
  refund: "Refund / cashback",
  income: "Income",
  income_reversal: "Income reversal",
};

const ENTRY_DIRECTION_TYPE: Record<EntryDirection, EditableTransactionType> = {
  spend: "spend",
  refund: "spend",
  income: "income",
  income_reversal: "income",
};

/** The stored `transaction_type` for a given entry direction — `refund` folds
 * into `spend` and `income_reversal` into `income` (ADR-0009). */
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
  if (t.transaction_type === "income") {
    return t.amount_paise < 0 ? "income_reversal" : "income";
  }
  return isRefund(t) ? "refund" : "spend";
}

/** Signed paise from a positive rupee magnitude, by entry direction — the
 * sign convention PRD §F4a pins and the backend re-validates via `sign_error`
 * (ADR-0007 rule 4, amended by ADR-0009): spend and income reversal are
 * negative, refund and income positive.
 *
 * `transfer` is not passed here: its legs' signs are server-derived. */
export function signedPaise(direction: EntryDirection, magnitude: number): number {
  return direction === "spend" || direction === "income_reversal"
    ? -magnitude
    : magnitude;
}
