"use client";

/**
 * Manual transfer between two accounts (PRD §F2). The user enters a positive
 * rupee magnitude + from/to accounts; the server derives the leg signs, labels,
 * and the pair link, and auto-confirms both legs. The board defaults to its
 * Spending view, so neither leg appears where the user is standing (they are
 * reachable, but only via the Transfers type filter) — this dialog confirms the
 * result inline, since without it a transfer would submit into silence.
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { Sensitive } from "@/components/balance-visibility";
import { IconCheck } from "@/components/icons";
import {
  ApiError,
  createTransfer,
  listAccounts,
  type AccountRead,
  type TransferRead,
} from "@/lib/api/client";
import { Field, PickerButton, TextInput } from "@/components/form/fields";
import { accountLabel } from "@/lib/accounts";
import { toLocalYMD } from "@/lib/dates";
import { formatINR, rupeesToPaise } from "@/lib/format";

export function TransferDialog({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const accountsQuery = useQuery({
    queryKey: ["accounts"],
    queryFn: listAccounts,
  });
  // Transfers can't involve investment accounts (backend 422s); don't offer them.
  const accounts = (accountsQuery.data ?? []).filter(
    (a) => a.type !== "investment",
  );

  const [sourceId, setSourceId] = useState<number | null>(null);
  const [destId, setDestId] = useState<number | null>(null);
  const [amount, setAmount] = useState("");
  const [date, setDate] = useState(toLocalYMD(new Date()));
  const [result, setResult] = useState<TransferRead | null>(null);

  const source = accounts.find((a) => a.id === sourceId) ?? null;
  const dest = accounts.find((a) => a.id === destId) ?? null;
  const magnitude = rupeesToPaise(amount);

  const mutation = useMutation({
    mutationFn: () =>
      createTransfer({
        date,
        source_account_id: sourceId!,
        dest_account_id: destId!,
        amount_paise: magnitude,
      }),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      queryClient.invalidateQueries({ queryKey: ["dashboards"] });
      setResult(data); // show the confirmation; do NOT auto-close
    },
  });

  const canSubmit =
    sourceId != null &&
    destId != null &&
    sourceId !== destId &&
    magnitude > 0 &&
    date !== "" &&
    !mutation.isPending;

  return (
    <Dialog open onOpenChange={(open) => (open ? null : onClose())}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Transfer between accounts</DialogTitle>
          <DialogDescription className="sr-only">
            Move a positive amount from one of your accounts to another; both
            legs are recorded automatically.
          </DialogDescription>
        </DialogHeader>

        {result ? (
          <SuccessView result={result} accounts={accounts} />
        ) : (
          <div className="grid gap-3">
            <Field label="From">
              <PickerButton
                label={source ? accountLabel(source) : "Source account"}
                muted={source == null}
              >
                {accounts.length === 0 ? (
                  <DropdownMenuItem disabled>
                    <span className="text-muted-foreground">
                      No accounts yet
                    </span>
                  </DropdownMenuItem>
                ) : (
                  accounts.map((a) => (
                    <DropdownMenuItem
                      key={a.id}
                      // Can't be both source and destination.
                      disabled={a.id === destId}
                      onSelect={() => setSourceId(a.id)}
                    >
                      {accountLabel(a)}
                    </DropdownMenuItem>
                  ))
                )}
              </PickerButton>
            </Field>

            <Field label="To">
              <PickerButton
                label={dest ? accountLabel(dest) : "Destination account"}
                muted={dest == null}
              >
                {accounts.length === 0 ? (
                  <DropdownMenuItem disabled>
                    <span className="text-muted-foreground">
                      No accounts yet
                    </span>
                  </DropdownMenuItem>
                ) : (
                  accounts.map((a) => (
                    <DropdownMenuItem
                      key={a.id}
                      // Can't transfer to the source account.
                      disabled={a.id === sourceId}
                      onSelect={() => setDestId(a.id)}
                    >
                      {accountLabel(a)}
                    </DropdownMenuItem>
                  ))
                )}
              </PickerButton>
            </Field>

            <div className="grid grid-cols-2 gap-3">
              <Field label="Amount (₹)">
                <TextInput
                  value={amount}
                  onChange={setAmount}
                  placeholder="0.00"
                  inputMode="decimal"
                  type="number"
                  step="0.01"
                  min="0"
                />
              </Field>
              <Field label="Date">
                <input
                  type="date"
                  value={date}
                  onChange={(e) => setDate(e.target.value)}
                  className="w-full rounded-md border border-border bg-background px-2.5 py-2 text-[12.5px] text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                />
              </Field>
            </div>
          </div>
        )}

        {mutation.isError ? (
          <p className="text-[12px] text-neg">
            {mutation.error instanceof ApiError
              ? mutation.error.detail
              : "Couldn’t record the transfer — try again."}
          </p>
        ) : null}

        <DialogFooter>
          {result ? (
            <Button
              type="button"
              className="h-8 px-3 text-[12.5px]"
              onClick={onClose}
            >
              Done
            </Button>
          ) : (
            <>
              <Button
                type="button"
                variant="ghost"
                className="h-8 px-3 text-[12.5px]"
                onClick={onClose}
                disabled={mutation.isPending}
              >
                Cancel
              </Button>
              <Button
                type="button"
                className="h-8 px-3 text-[12.5px]"
                onClick={() => mutation.mutate()}
                disabled={!canSubmit}
              >
                {mutation.isPending ? "Transferring…" : "Transfer"}
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function SuccessView({
  result,
  accounts,
}: {
  result: TransferRead;
  accounts: AccountRead[];
}) {
  // Names off the returned legs (authoritative post-write), not the form state.
  const nameOf = (id: number) =>
    accounts.find((a) => a.id === id)?.name ?? "account";
  const amount = formatINR(Math.abs(result.source.amount_paise));

  return (
    <div className="flex items-start gap-2 py-2 text-[13px] text-foreground">
      <IconCheck className="mt-0.5 size-4 shrink-0 text-pos" />
      <span>
        Transfer recorded — <Sensitive>{amount}</Sensitive> from{" "}
        <span className="font-medium">{nameOf(result.source.account_id)}</span>{" "}
        to <span className="font-medium">{nameOf(result.dest.account_id)}</span>
        .
      </span>
    </div>
  );
}
