"use client";

/**
 * Manual transaction entry (PRD §F2) — the "Add" control in the /expenses
 * SubNav trailing slot plus its entry dialog. Spend/refund/income here (transfer
 * has its own dialog).
 *
 * The amount is entered as a positive rupee magnitude; the sign is applied by
 * type (spend negative, refund/income positive — PRD §F4a), mirroring the
 * accounts create form. A created row is auto-confirmed server-side, but the
 * board only shows spend+refund by default (and filters by account/category/
 * month), so a new row may be out of view — income especially — and the dialog
 * confirms inline rather than relying on the row appearing.
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
import { IconCheck, IconExchange, IconPlus } from "@/components/icons";
import { LabelInput } from "@/components/labels/label-input";
import {
  ApiError,
  createTransaction,
  listAccounts,
  listCategories,
  type TransactionCreate,
} from "@/lib/api/client";
import { Field, PickerButton, TextInput } from "@/components/form/fields";
import { rupeesToPaise } from "@/lib/format";
import { accountLabel } from "@/lib/accounts";
import { categoryKindForType } from "@/lib/categories";
import {
  EDITABLE_TRANSACTION_TYPES,
  TRANSACTION_TYPE_LABELS,
  signedPaise,
  type EditableTransactionType,
} from "@/lib/transaction-types";
import { toLocalYMD } from "@/lib/dates";
import { invalidateRules } from "@/lib/queries/invalidate";
import { TransferDialog } from "./transfer-dialog";

export function AddControls() {
  const [entryOpen, setEntryOpen] = useState(false);
  const [transferOpen, setTransferOpen] = useState(false);

  return (
    <>
      <Button
        type="button"
        variant="outline"
        onClick={() => setEntryOpen(true)}
        className="h-7 gap-1.5 px-2.5 text-[12px] font-medium"
      >
        <IconPlus className="size-3" />
        Add
      </Button>
      <Button
        type="button"
        variant="outline"
        onClick={() => setTransferOpen(true)}
        className="h-7 gap-1.5 px-2.5 text-[12px] font-medium"
      >
        <IconExchange className="size-3" />
        Transfer
      </Button>

      {entryOpen ? <EntryDialog onClose={() => setEntryOpen(false)} /> : null}
      {transferOpen ? (
        <TransferDialog onClose={() => setTransferOpen(false)} />
      ) : null}
    </>
  );
}

function EntryDialog({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const accountsQuery = useQuery({
    queryKey: ["accounts"],
    queryFn: listAccounts,
  });
  const categoriesQuery = useQuery({
    queryKey: ["categories"],
    queryFn: listCategories,
  });
  // Never offer an account the backend will reject (investment can't hold
  // transactions; the list endpoint already omits archived).
  const accounts = (accountsQuery.data ?? []).filter(
    (a) => a.type !== "investment",
  );
  const categories = categoriesQuery.data ?? [];

  const [type, setType] = useState<EditableTransactionType>("spend");
  const [accountId, setAccountId] = useState<number | null>(null);
  const [amount, setAmount] = useState("");
  const [merchant, setMerchant] = useState("");
  const [date, setDate] = useState(toLocalYMD(new Date()));
  const [categoryId, setCategoryId] = useState<number | null>(null);
  const [labels, setLabels] = useState<string[]>([]);
  const [justAdded, setJustAdded] = useState<string | null>(null);

  const selectedAccount = accounts.find((a) => a.id === accountId) ?? null;
  // Lookup stays over the full list; only the picker options are kind-filtered.
  const selectedCategory = categories.find((c) => c.id === categoryId) ?? null;
  const categoryKind = categoryKindForType(type);
  const visibleCategories = categories.filter((c) => c.kind === categoryKind);
  // Single source for both the guard and the request body.
  const magnitude = rupeesToPaise(amount);

  // Switching type swaps the category scope; drop a now-mismatched selection
  // so an income category can't ride along on a spend row (and vice versa).
  function handleTypeChange(next: EditableTransactionType) {
    setType(next);
    if (
      selectedCategory &&
      selectedCategory.kind !== categoryKindForType(next)
    ) {
      setCategoryId(null);
    }
  }

  const mutation = useMutation({
    mutationFn: () => {
      const body: TransactionCreate = {
        date,
        account_id: accountId!,
        amount_paise: signedPaise(type, magnitude),
        transaction_type: type,
        merchant_raw: merchant.trim() || null,
        category_id: categoryId,
        ...(labels.length > 0 ? { labels } : {}),
      };
      return createTransaction(body);
    },
    onMutate: () => setJustAdded(null), // clear a prior success line on retry
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      queryClient.invalidateQueries({ queryKey: ["dashboards"] });
      // A new tag typed here is get-or-created server-side; refresh the catalog
      // so the board filter / Settings / autocomplete pick it up immediately.
      queryClient.invalidateQueries({ queryKey: ["labels"] });
      // A board create runs learn_merchant_memory (F3/F3a) server-side, so
      // refresh /settings/rules + tagging-health + candidate confidence.
      invalidateRules(queryClient);
      // Stay open for rapid multi-entry: confirm inline, reset the per-row
      // fields, keep account/type/date for the next one.
      setJustAdded(merchant.trim());
      setAmount("");
      setMerchant("");
      setCategoryId(null);
      setLabels([]);
    },
  });

  const canSubmit =
    accountId != null && magnitude > 0 && date !== "" && !mutation.isPending;

  // Name the client gate that's holding "Add" disabled, rather than leaving the
  // button inert with no stated reason. Account first: nothing auto-selects one,
  // so a freshly opened dialog is blocked on the account, not the amount. `date`
  // is pre-filled and stays unhinted (CLAUDE.md §2). The client hint outranks
  // the API error, mirroring security-manager.tsx:107-127 — but NOT the
  // just-added confirmation: onSuccess clears the amount for rapid multi-entry,
  // so an unguarded hint would replace "Add another, or Done." the instant a row
  // saved. Same truthiness test the success line uses, so the two never disagree.
  const hint = justAdded
    ? null
    : accountId == null
      ? "Select an account to add this transaction."
      : magnitude <= 0
        ? "Enter an amount greater than ₹0."
        : null;

  return (
    <Dialog open onOpenChange={(open) => (open ? null : onClose())}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add transaction</DialogTitle>
          <DialogDescription className="sr-only">
            Record a spend, refund, or income entry against one of your
            accounts.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-3">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Type">
              <PickerButton label={TRANSACTION_TYPE_LABELS[type]}>
                {EDITABLE_TRANSACTION_TYPES.map((t) => (
                  <DropdownMenuItem
                    key={t}
                    onSelect={() => handleTypeChange(t)}
                  >
                    {TRANSACTION_TYPE_LABELS[t]}
                  </DropdownMenuItem>
                ))}
              </PickerButton>
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

          <Field label="Account">
            <PickerButton
              label={
                selectedAccount
                  ? accountLabel(selectedAccount)
                  : "Select an account"
              }
              muted={selectedAccount == null}
            >
              {accounts.length === 0 ? (
                <DropdownMenuItem disabled>
                  <span className="text-muted-foreground">No accounts yet</span>
                </DropdownMenuItem>
              ) : (
                accounts.map((a) => (
                  <DropdownMenuItem
                    key={a.id}
                    onSelect={() => setAccountId(a.id)}
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
            <Field label="Category (optional)">
              <PickerButton
                label={
                  selectedCategory ? selectedCategory.name : "Uncategorized"
                }
                muted={selectedCategory == null}
              >
                <DropdownMenuItem onSelect={() => setCategoryId(null)}>
                  <span className="text-muted-foreground">Uncategorized</span>
                </DropdownMenuItem>
                {visibleCategories.map((c) => (
                  <DropdownMenuItem
                    key={c.id}
                    onSelect={() => setCategoryId(c.id)}
                  >
                    {c.name}
                  </DropdownMenuItem>
                ))}
              </PickerButton>
            </Field>
          </div>

          <Field
            label={
              type === "income" ? "Source (optional)" : "Merchant (optional)"
            }
          >
            <TextInput
              value={merchant}
              onChange={setMerchant}
              placeholder={
                type === "income"
                  ? "e.g. Acme Corp (salary)"
                  : "e.g. Auto rickshaw"
              }
              maxLength={256}
            />
          </Field>

          <LabelInput
            label="Tags (optional)"
            value={labels}
            onChange={setLabels}
          />
        </div>

        {hint ? (
          <p className="text-[12px] text-neg">{hint}</p>
        ) : mutation.isError ? (
          <p className="text-[12px] text-neg">
            {mutation.error instanceof ApiError
              ? mutation.error.detail
              : "Couldn’t save — try again."}
          </p>
        ) : justAdded ? (
          <p className="inline-flex items-center gap-1.5 text-[12px] text-pos">
            <IconCheck className="size-3.5" />
            Added “{justAdded}”. Add another, or Done.
          </p>
        ) : null}

        <DialogFooter>
          <Button
            type="button"
            variant="ghost"
            className="h-8 px-3 text-[12.5px]"
            onClick={onClose}
            disabled={mutation.isPending}
          >
            Done
          </Button>
          <Button
            type="button"
            className="h-8 px-3 text-[12.5px]"
            onClick={() => mutation.mutate()}
            disabled={!canSubmit}
          >
            {mutation.isPending ? "Adding…" : "Add"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
