"use client";

/**
 * "Refund of" picker inside the transaction dialog (ADR-0009 rule 13): links a refund to
 * the spend it reverses. The link is informational (nothing is re-summed), so it has its
 * own endpoints and its own mutations, separate from the dialog's PATCH, and the dialog
 * stays open after a pick.
 *
 * The candidate list is the server's: it already holds exactly the rows `PUT
 * .../refund-link` accepts, ranked (current original, same merchant, same amount, nearest
 * date). The client never re-derives which originals are valid. The query sits under
 * ["transactions"] on purpose, so the list refreshes after any board write.
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Sensitive } from "@/components/balance-visibility";
import { Field, PickerButton } from "@/components/form/fields";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import {
  ApiError,
  deleteRefundLink,
  listRefundCandidates,
  setRefundLink,
  type TransactionRead,
} from "@/lib/api/client";
import { formatDateWithYear, formatINR } from "@/lib/format";

export function RefundOriginalField({ txn }: { txn: TransactionRead }) {
  const queryClient = useQueryClient();
  // `txn` is the board's snapshot, so it cannot reflect a link made in this dialog.
  const [linkedId, setLinkedId] = useState<number | null>(txn.refund_of_id);

  const candidates = useQuery({
    queryKey: ["transactions", "refund-candidates", txn.id],
    queryFn: () => listRefundCandidates(txn.id),
  });

  const link = useMutation({
    mutationFn: (originalId: number | null) =>
      originalId == null ? deleteRefundLink(txn.id) : setRefundLink(txn.id, originalId),
    onSuccess: (_data, originalId) => {
      setLinkedId(originalId);
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
    },
  });

  const rows = candidates.data ?? [];
  const linked = linkedId == null ? null : (rows.find((r) => r.id === linkedId) ?? null);
  const label =
    linkedId == null
      ? "Not linked"
      : linked != null
        ? `${formatDateWithYear(linked.date)} · ${linked.merchant_raw ?? "Unnamed"}`
        : "Linked";

  return (
    <div className="grid gap-1">
      <Field label="Refund of">
        <PickerButton label={label} muted={linkedId == null} disabled={link.isPending}>
          {linkedId != null ? (
            <DropdownMenuItem onSelect={() => link.mutate(null)}>Remove link</DropdownMenuItem>
          ) : null}
          {rows.length === 0 ? (
            <DropdownMenuItem disabled>
              <span className="text-muted-foreground">No matching purchases</span>
            </DropdownMenuItem>
          ) : (
            rows.map((r) => (
              <DropdownMenuItem key={r.id} onSelect={() => link.mutate(r.id)}>
                <span className="flex w-full items-center justify-between gap-3">
                  <span>
                    {formatDateWithYear(r.date)} · {r.merchant_raw ?? "Unnamed"}
                  </span>
                  <Sensitive>{formatINR(Math.abs(r.amount_paise))}</Sensitive>
                </span>
              </DropdownMenuItem>
            ))
          )}
        </PickerButton>
      </Field>
      {link.isError ? (
        <p className="text-[12px] text-neg">
          {link.error instanceof ApiError ? link.error.detail : "Couldn’t link — try again."}
        </p>
      ) : null}
    </div>
  );
}
