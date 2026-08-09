import type { AccountRead } from "@/lib/api/client";

/** Display label for an account: "Name ··1234" when a last-4 is set, else "Name". */
export function accountLabel(a: AccountRead): string {
  return a.last4 ? `${a.name} ··${a.last4}` : a.name;
}

/** Label for a pending import batch: "Name ··1234", falling back to the name
 * alone, then a generic "Import" when the batch is account-less. Shared by the
 * top-bar notification bell and the /imports/review index. */
export function pendingBatchLabel(
  name: string | null,
  last4: string | null,
): string {
  if (!name) return "Import";
  return last4 ? `${name} ··${last4}` : name;
}
