"use client";

/**
 * "Paid your card bill elsewhere?" picker inside the transaction dialog (ADR-0002 rule
 * 11b): pairs a card's bill credit with the bank payment that settled it, when the
 * importer did not link them. The candidate list is the server's, rendered as returned
 * (nearest date first, never filtered or re-sorted here). A link closes the dialog,
 * because the row's type and category change server-side and neither is on the wire.
 *
 * The query sits under ["transactions"] on purpose, so the list refreshes after any
 * board write. A link never teaches (ADR-0004), so no rule cache is invalidated.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Sensitive } from "@/components/balance-visibility";
import { Field, PickerButton } from "@/components/form/fields";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { accountLabel } from "@/lib/accounts";
import {
  ApiError,
  linkTransaction,
  listLinkCandidates,
  type AccountRead,
  type TransactionRead,
} from "@/lib/api/client";
import { formatDateWithYear, formatINR } from "@/lib/format";

export function CardBillLinkField({
  txn,
  accounts,
  onLinked,
}: {
  txn: TransactionRead;
  accounts: AccountRead[];
  onLinked: () => void;
}) {
  const queryClient = useQueryClient();

  const candidates = useQuery({
    queryKey: ["transactions", "link-candidates", txn.id],
    queryFn: () => listLinkCandidates(txn.id),
  });

  const link = useMutation({
    mutationFn: (partnerId: number) => linkTransaction(txn.id, partnerId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      queryClient.invalidateQueries({ queryKey: ["dashboards"] });
      onLinked();
    },
  });

  const rows = candidates.data ?? [];

  return (
    <div className="grid gap-1">
      <Field label="Paid your card bill elsewhere?">
        <PickerButton label="Pick the payment" muted disabled={link.isPending}>
          {rows.length === 0 ? (
            <DropdownMenuItem disabled>
              <span className="text-muted-foreground">No matching payment found</span>
            </DropdownMenuItem>
          ) : (
            rows.map((r) => {
              const acct = accounts.find((a) => a.id === r.account_id);
              return (
                <DropdownMenuItem key={r.id} onSelect={() => link.mutate(r.id)}>
                  <span className="flex w-full items-center justify-between gap-3">
                    <span>
                      {formatDateWithYear(r.date)} · {r.merchant_raw ?? "Unnamed"}
                      {acct ? ` · ${accountLabel(acct)}` : ""}
                    </span>
                    <Sensitive>{formatINR(Math.abs(r.amount_paise))}</Sensitive>
                  </span>
                </DropdownMenuItem>
              );
            })
          )}
        </PickerButton>
      </Field>
      <p className="text-[12px] text-muted-foreground">
        Pick the bank payment that paid this bill. Both become a transfer; Break link undoes it.
      </p>
      {link.isError ? (
        <p className="text-[12px] text-neg">
          {link.error instanceof ApiError ? link.error.detail : "Couldn’t link — try again."}
        </p>
      ) : null}
    </div>
  );
}
