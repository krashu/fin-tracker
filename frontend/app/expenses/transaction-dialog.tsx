"use client";

/**
 * Transaction-detail editor. Click a row on /expenses → this dialog.
 *
 * Every user-visible column is editable (ADR-0007): date, amount, type, merchant,
 * account, category and tags. The dedup identity is an implementation detail and
 * must never surface as a UI constraint — which is also the only way to fix a
 * credit the parser could not classify, since it can never tell a merchant refund
 * from a cashback credit and the type is load-bearing for every spend aggregate.
 *
 * Two things the backend enforces that this dialog has to explain on screen:
 *
 * - Editing date / amount / merchant / account recomputes the PRD §F4 fingerprint,
 *   so a save can come back 409 "transaction already exists". Rendered inline like
 *   any other error; no client-side prediction of it.
 * - A row linked as one leg of a transfer pair freezes those fields plus the type
 *   until the link is broken (rule 7), because the two legs are one movement of
 *   money with server-derived signs. The banner says so and offers the unlink, so
 *   the user never meets that 422 blind.
 *
 * Amount is entered as a positive rupee magnitude and signed by type, mirroring
 * add-transaction.tsx. It is deliberately NOT wrapped in `Sensitive`: you cannot
 * type into `••••`, and the entry form sets the same precedent — masking covers
 * the board, not the field you opened to edit.
 *
 * Save sends only the changed fields, then invalidates ["transactions"] so the
 * board re-fetches (PRD §F9 propagation — no SSE in v1).
 */
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

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
import { LabelInput } from "@/components/labels/label-input";
import { Field, PickerButton, TextInput } from "@/components/form/fields";
import {
  ApiError,
  patchTransaction,
  unlinkTransaction,
  type AccountRead,
  type CategoryRead,
  type TransactionRead,
  type TransactionUpdate,
} from "@/lib/api/client";
import { accountLabel } from "@/lib/accounts";
import { categoryKindForType } from "@/lib/categories";
import { paiseToRupees, rupeesToPaise } from "@/lib/format";
import { sameLabelSet } from "@/lib/labels";
import { invalidateRules } from "@/lib/queries/invalidate";
import {
  EDITABLE_TRANSACTION_TYPES,
  TRANSACTION_TYPE_LABELS,
  signedPaise,
  type EditableTransactionType,
} from "@/lib/transaction-types";

const UNCATEGORIZED = "Uncategorized";

export function TransactionDialog({
  txn,
  categories,
  accounts,
  accountLabelText,
  onClose,
}: {
  txn: TransactionRead;
  categories: CategoryRead[];
  accounts: AccountRead[];
  accountLabelText: string;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();

  // A transfer leg keeps its type in state so the picker can render "Transfer",
  // but that type is never selectable and the field is disabled while paired.
  const [type, setType] = useState(txn.transaction_type);
  const [date, setDate] = useState(txn.date);
  const [amount, setAmount] = useState(paiseToRupees(Math.abs(txn.amount_paise)));
  const [merchant, setMerchant] = useState(txn.merchant_raw ?? "");
  const [accountId, setAccountId] = useState(txn.account_id);
  const [categoryId, setCategoryId] = useState<number | null>(txn.category_id);
  const [labels, setLabels] = useState<string[]>(txn.labels.map((l) => l.name));

  function invalidateBoard() {
    queryClient.invalidateQueries({ queryKey: ["transactions"] });
    // Recategorizing or re-typing changes the SummaryStrip's per-month total
    // (PRD §F9); a mounted dashboards query needs an explicit nudge to refetch.
    queryClient.invalidateQueries({ queryKey: ["dashboards"] });
  }

  const mutation = useMutation({
    mutationFn: (body: TransactionUpdate) => patchTransaction(txn.id, body),
    onSuccess: () => {
      invalidateBoard();
      // A newly-typed tag is get-or-created server-side; refresh the catalog.
      queryClient.invalidateQueries({ queryKey: ["labels"] });
      // A board edit runs learn_merchant_memory (F3/F3a) server-side, so refresh
      // /settings/rules + tagging-health + candidate confidence.
      invalidateRules(queryClient);
      onClose();
    },
  });

  // Unlink is its own mutation: it is a separate endpoint, it must not be
  // batched into the PATCH (the backend rejects an identity edit on a still-
  // paired row), and the dialog stays open afterwards so the now-unfrozen fields
  // become editable in place.
  const unlink = useMutation({
    mutationFn: () => unlinkTransaction(txn.id),
    onSuccess: invalidateBoard,
  });

  // `txn` is the board's row snapshot, so it still reads as paired after a
  // successful unlink — invalidating ["transactions"] refetches the list behind us
  // but cannot rewrite this prop. Fold the mutation's own result in, so the fields
  // unfreeze in place instead of making the user close and reopen the dialog.
  const isPaired = txn.transfer_pair_id != null && !unlink.isSuccess;

  const magnitude = rupeesToPaise(amount);
  const mergedType = type;
  const categoryKind = categoryKindForType(mergedType);
  // Lookup stays over the full list; only the picker options are kind-filtered.
  const selectedCategory = categories.find((c) => c.id === categoryId) ?? null;
  const visibleCategories = categories.filter((c) => c.kind === categoryKind);
  // GET /accounts serves active accounts only, so a row on an ARCHIVED account
  // has no option to select. Keep the picker honest: show the stored label as the
  // current value and let the user move the row off the archived account, rather
  // than silently re-pointing it at whatever happens to be first in the list.
  const selectedAccount = accounts.find((a) => a.id === accountId) ?? null;
  const accountText =
    selectedAccount != null ? accountLabel(selectedAccount) : accountLabelText;

  // Switching type swaps the category scope; drop a now-mismatched selection so an
  // income category can't ride along on a spend row. The backend 422s on a kind
  // flip that keeps an incompatible category (ADR-0007 rule 5) — clearing here is
  // what turns that into one round-trip instead of an error.
  function handleTypeChange(next: EditableTransactionType) {
    setType(next);
    if (selectedCategory && selectedCategory.kind !== categoryKindForType(next)) {
      setCategoryId(null);
    }
  }

  const nextMerchant = merchant.trim() || null;
  // A transfer keeps its OWN sign rather than deriving one from the type: the
  // pair's signs are server-derived (ADR-0002, source out / dest in), so the dialog
  // must never re-sign a leg. Preserving the sign and applying the typed magnitude
  // leaves a paired row's amount untouched (the input is disabled, so the magnitude
  // still equals the stored one) while keeping an UNPAIRED transfer — a legal
  // survivor of a delete or an unlink — fully editable. `transfer` is never
  // reachable as a NEW type; it isn't in EDITABLE_TRANSACTION_TYPES.
  const nextAmount =
    mergedType === "transfer"
      ? (txn.amount_paise < 0 ? -magnitude : magnitude)
      : signedPaise(mergedType, magnitude);

  const changed = {
    date: date !== txn.date,
    amount_paise: nextAmount !== txn.amount_paise,
    transaction_type: mergedType !== txn.transaction_type,
    merchant_raw: nextMerchant !== txn.merchant_raw,
    account_id: accountId !== txn.account_id,
    category_id: categoryId !== txn.category_id,
    labels: !sameLabelSet(labels, txn.labels.map((l) => l.name)),
  };
  const hasChanges = Object.values(changed).some(Boolean);
  const canSave = hasChanges && magnitude > 0 && date !== "" && !mutation.isPending;

  function handleSave() {
    // Minimal PATCH — an unchanged field isn't sent, so the backend's
    // did-an-identity-input-actually-change branch stays a no-op for a
    // category-only edit and `occurrence` is never needlessly reset.
    const body: TransactionUpdate = {};
    if (changed.date) body.date = date;
    if (changed.amount_paise) body.amount_paise = nextAmount;
    if (changed.transaction_type) {
      body.transaction_type = mergedType;
    }
    if (changed.merchant_raw) body.merchant_raw = nextMerchant;
    if (changed.account_id) body.account_id = accountId;
    if (changed.category_id) body.category_id = categoryId;
    if (changed.labels) body.labels = labels;
    // A kind flip must carry the category in the SAME request (rule 5), even when
    // the value itself didn't change — an unchanged null still has to be stated.
    if (
      changed.transaction_type &&
      categoryKindForType(txn.transaction_type) !== categoryKind &&
      body.category_id === undefined
    ) {
      body.category_id = categoryId;
    }
    mutation.mutate(body);
  }

  const busy = mutation.isPending || unlink.isPending;
  const errored = mutation.isError ? mutation.error : unlink.error;

  return (
    <Dialog open onOpenChange={(open) => (open ? null : onClose())}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Edit transaction</DialogTitle>
          <DialogDescription className="sr-only">
            Correct any field on this transaction, including its amount, date,
            merchant and account.
          </DialogDescription>
        </DialogHeader>

        {isPaired ? (
          <div className="flex items-start justify-between gap-3 rounded-md border border-border bg-muted/50 px-3 py-2">
            <p className="text-[12px] text-foreground/80">
              <span className="font-medium">Linked CC bill payment.</span> This
              row is one leg of a transfer, so its amount, date, merchant,
              account and type are locked. Break the link to edit them.
            </p>
            <Button
              type="button"
              variant="outline"
              className="h-7 shrink-0 px-2 text-[12px]"
              onClick={() => unlink.mutate()}
              disabled={busy}
            >
              {unlink.isPending ? "Breaking…" : "Break link"}
            </Button>
          </div>
        ) : null}

        <div className="grid gap-3">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Type">
              <PickerButton
                label={TRANSACTION_TYPE_LABELS[mergedType]}
                disabled={isPaired}
              >
                {EDITABLE_TRANSACTION_TYPES.map((t) => (
                  <DropdownMenuItem key={t} onSelect={() => handleTypeChange(t)}>
                    {TRANSACTION_TYPE_LABELS[t]}
                  </DropdownMenuItem>
                ))}
              </PickerButton>
            </Field>
            <Field label="Date">
              <input
                type="date"
                value={date}
                disabled={isPaired}
                onChange={(e) => setDate(e.target.value)}
                className="w-full rounded-md border border-border bg-background px-2.5 py-2 text-[12.5px] text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
              />
            </Field>
          </div>

          <Field label="Account">
            <PickerButton label={accountText} disabled={isPaired}>
              {accounts.length === 0 ? (
                <DropdownMenuItem disabled>
                  <span className="text-muted-foreground">No accounts yet</span>
                </DropdownMenuItem>
              ) : (
                accounts.map((a) => (
                  <DropdownMenuItem key={a.id} onSelect={() => setAccountId(a.id)}>
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
                disabled={isPaired}
              />
            </Field>
            <Field label="Category">
              <PickerButton
                label={selectedCategory ? selectedCategory.name : UNCATEGORIZED}
                muted={selectedCategory == null}
              >
                <DropdownMenuItem onSelect={() => setCategoryId(null)}>
                  <span className="text-muted-foreground">{UNCATEGORIZED}</span>
                </DropdownMenuItem>
                {visibleCategories.map((c) => (
                  <DropdownMenuItem key={c.id} onSelect={() => setCategoryId(c.id)}>
                    {c.name}
                  </DropdownMenuItem>
                ))}
              </PickerButton>
            </Field>
          </div>

          <Field label={mergedType === "income" ? "Source" : "Merchant"}>
            <TextInput
              value={merchant}
              onChange={setMerchant}
              placeholder="e.g. Auto rickshaw"
              maxLength={256}
              disabled={isPaired}
            />
          </Field>

          <LabelInput label="Tags" value={labels} onChange={setLabels} />
        </div>

        {errored ? (
          <p className="text-[12px] text-neg">
            {errored instanceof ApiError
              ? errored.detail
              : "Couldn’t save — try again."}
          </p>
        ) : null}

        <DialogFooter>
          <Button
            type="button"
            variant="ghost"
            className="h-8 px-3 text-[12.5px]"
            onClick={onClose}
            disabled={busy}
          >
            Cancel
          </Button>
          <Button
            type="button"
            className="h-8 px-3 text-[12.5px]"
            onClick={handleSave}
            disabled={!canSave || busy}
          >
            {mutation.isPending ? "Saving…" : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
