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
 *   money with server-derived signs. The banner says so and offers the unlink, which closes
 *   the dialog (the restored type and category are server-side), so the user never
 *   meets that 422 blind.
 *
 * A row the server marks `card_bill_link_offered` offers the manual card-bill link
 * (ADR-0002 rule 11b); a link closes the dialog.
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
import { CardBillLinkField } from "./card-bill-link-field";
import { RefundOriginalField } from "./refund-original-field";
import { Field, PickerButton, TextInput } from "@/components/form/fields";
import { CategorySelector } from "@/components/categories/category-selector";
import { IconCheck, IconUsers } from "@/components/icons";
import { SplitDialog } from "@/components/friends/split-dialog";
import { LinkRepaymentDialog } from "@/components/friends/link-repayment-dialog";
import {
  ApiError,
  patchTransaction,
  unlinkTransaction,
  type AccountRead,
  type CategoryRead,
  type TransactionRead,
  type TransactionType,
  type TransactionUpdate,
} from "@/lib/api/client";
import { accountLabel } from "@/lib/accounts";
import { categoryKindForType } from "@/lib/categories";
import { paiseToRupees, rupeesToPaise } from "@/lib/format";
import { sameLabelSet } from "@/lib/labels";
import { invalidateRules } from "@/lib/queries/invalidate";
import {
  ENTRY_DIRECTIONS,
  ENTRY_DIRECTION_LABELS,
  TRANSACTION_TYPE_LABELS,
  directionForTransaction,
  isRefund,
  directionToType,
  signedPaise,
  type EntryDirection,
} from "@/lib/transaction-types";

const UNCATEGORIZED = "Uncategorized";

export function TransactionDialog({
  txn,
  categories,
  accounts,
  accountLabelText,
  onClose,
  onCategoryCorrected,
}: {
  txn: TransactionRead;
  categories: CategoryRead[];
  accounts: AccountRead[];
  accountLabelText: string;
  onClose: () => void;
  /** Called after a save that changed the category, so the board can offer to apply
   * the correction to past rows of the same merchant (ADR-0004 rule 4). */
  onCategoryCorrected?: (txnId: number) => void;
}) {
  const queryClient = useQueryClient();

  // A transfer row keeps "transfer" in state: the picker offers it as a fifth
  // choice after the four directions (ADR-0002 rule 7d), and the field is
  // disabled while paired. Any other row seeds an EntryDirection (ADR-0009: a
  // refund is not its own stored type, so the picker's four-way Spend/Refund/
  // Income/Income reversal choice ("Refund / cashback" is the refund direction,
  // ADR-0013) has to be recovered from {type, sign} via `directionForTransaction`).
  const [entry, setEntry] = useState<EntryDirection | "transfer">(() =>
    txn.transaction_type === "transfer"
      ? "transfer"
      : directionForTransaction(txn),
  );
  const [date, setDate] = useState(txn.date);
  const [amount, setAmount] = useState(paiseToRupees(Math.abs(txn.amount_paise)));
  const [merchant, setMerchant] = useState(txn.merchant_raw ?? "");
  const [accountId, setAccountId] = useState(txn.account_id);
  const [categoryId, setCategoryId] = useState<number | null>(txn.category_id);
  const [labels, setLabels] = useState<string[]>(txn.labels.map((l) => l.name));
  const [splitOpen, setSplitOpen] = useState(false);
  const [repaymentOpen, setRepaymentOpen] = useState(false);

  function invalidateBoard() {
    queryClient.invalidateQueries({ queryKey: ["transactions"] });
    // Recategorizing or re-typing changes the SummaryStrip's per-month total
    // (PRD §F9); a mounted dashboards query needs an explicit nudge to refetch.
    queryClient.invalidateQueries({ queryKey: ["dashboards"] });
  }

  const mutation = useMutation({
    mutationFn: (body: TransactionUpdate) => patchTransaction(txn.id, body),
    onSuccess: (_data, body) => {
      invalidateBoard();
      // A newly-typed tag is get-or-created server-side; refresh the catalog.
      queryClient.invalidateQueries({ queryKey: ["labels"] });
      // A board edit runs learn_merchant_memory (F3/F3a) server-side, so refresh
      // /settings/rules + tagging-health + candidate confidence.
      invalidateRules(queryClient);
      if (body.category_id != null && body.category_id !== txn.category_id) {
        onCategoryCorrected?.(txn.id);
      }
      onClose();
    },
  });

  // Unlink is its own mutation: it is a separate endpoint, and it must not be
  // batched into the PATCH (the backend rejects an identity edit on a still-
  // paired row). Unlink restores each leg's pre-link type and category server-side,
  // and neither is on the wire, so this dialog's state would be stale: it closes,
  // and the board refetches the row.
  const unlink = useMutation({
    mutationFn: () => unlinkTransaction(txn.id),
    onSuccess: () => {
      invalidateBoard();
      onClose();
    },
  });

  const isPaired = txn.transfer_pair_id != null;

  const magnitude = rupeesToPaise(amount);
  const mergedType: TransactionType =
    entry === "transfer" ? "transfer" : directionToType(entry);
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

  // Switching direction swaps the category scope; drop a now-mismatched
  // selection so an income category can't ride along on a spend row. The
  // backend 422s on a kind flip that keeps an incompatible category (ADR-0007
  // rule 5) — clearing here is what turns that into one round-trip instead of
  // an error. Spend → refund is a no-op here — both share the spend scope.
  function handleDirectionChange(next: EntryDirection | "transfer") {
    setEntry(next);
    if (
      selectedCategory &&
      selectedCategory.kind !==
        categoryKindForType(next === "transfer" ? "transfer" : directionToType(next))
    ) {
      setCategoryId(null);
    }
  }

  const nextMerchant = merchant.trim() || null;
  // A transfer keeps its OWN sign rather than deriving one from the type: the
  // pair's signs are server-derived (ADR-0002, source out / dest in), so the dialog
  // must never re-sign a leg. Preserving the sign and applying the typed magnitude
  // leaves a paired row's amount untouched (the input is disabled, so the magnitude
  // still equals the stored one) while keeping an UNPAIRED transfer — a legal
  // survivor of a delete or an unlink, or an unlinked card-bill debit (ADR-0002 rule 7b) —
  // fully editable, and a row retyped to Transfer here (rule 7d) keeps the sign it had.
  const nextAmount =
    entry === "transfer"
      ? (txn.amount_paise < 0 ? -magnitude : magnitude)
      : signedPaise(entry, magnitude);

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
    <>
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
                label={
                  entry === "transfer"
                    ? TRANSACTION_TYPE_LABELS.transfer
                    : ENTRY_DIRECTION_LABELS[entry]
                }
                disabled={isPaired}
              >
                {ENTRY_DIRECTIONS.map((d) => (
                  <DropdownMenuItem key={d} onSelect={() => handleDirectionChange(d)}>
                    {ENTRY_DIRECTION_LABELS[d]}
                  </DropdownMenuItem>
                ))}
                <DropdownMenuItem onSelect={() => handleDirectionChange("transfer")}>
                  {TRANSACTION_TYPE_LABELS.transfer}
                </DropdownMenuItem>
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

          {mergedType === "transfer" && !isPaired ? (
            <p className="text-[11px] text-muted-foreground">
              Transfers count in neither spend nor income.
            </p>
          ) : null}

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
              <CategorySelector
                value={categoryId}
                onChange={setCategoryId}
                categories={categories}
                kind={categoryKind}
                disabled={isPaired}
                placeholder={UNCATEGORIZED}
              />
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

          {txn.amount_paise > 0 ? (
            <div className="flex items-center justify-between rounded-md border border-border bg-muted/40 px-3 py-2">
              <div className="flex flex-col">
                <span className="text-[12px] font-medium text-foreground">
                  Friend Repayment
                </span>
                <span className="text-[11px] text-muted-foreground">
                  Link this incoming credit as a friend repayment.
                </span>
              </div>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-7 shrink-0 gap-1.5 px-2 text-[12px]"
                onClick={() => setRepaymentOpen(true)}
              >
                <IconCheck className="size-3.5 text-emerald-600 dark:text-emerald-400" />
                Link as Friend Repayment
              </Button>
            </div>
          ) : (
            <div className="flex items-center justify-between rounded-md border border-border bg-muted/40 px-3 py-2">
              <div className="flex flex-col">
                <span className="text-[12px] font-medium text-foreground">
                  Split / Front for friend
                </span>
                <span className="text-[11px] text-muted-foreground">
                  Track who owes you or whom you owe for this transaction.
                </span>
              </div>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-7 shrink-0 gap-1.5 px-2 text-[12px]"
                onClick={() => setSplitOpen(true)}
              >
                <IconUsers className="size-3.5" />
                Split with Friend
              </Button>
            </div>
          )}

          {isRefund(txn) ? <RefundOriginalField txn={txn} /> : null}
          {txn.card_bill_link_offered ? (
            <CardBillLinkField txn={txn} accounts={accounts} onLinked={onClose} />
          ) : null}
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
    {splitOpen ? (
      <SplitDialog
        transaction={txn}
        open={splitOpen}
        onClose={() => setSplitOpen(false)}
      />
    ) : null}
    {repaymentOpen ? (
      <LinkRepaymentDialog
        transaction={txn}
        open={repaymentOpen}
        onClose={() => setRepaymentOpen(false)}
      />
    ) : null}
    </>
  );
}
