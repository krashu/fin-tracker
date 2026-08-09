"use client";

/**
 * Account list + CRUD (PRD §F6). Reads/writes the shared ["accounts"] query the
 * import picker and expenses filter also consume, so a create/edit/archive
 * propagates everywhere on invalidate (PRD §F9).
 *
 * `type`/`currency`/`opening_balance_paise` are set once at create and locked
 * after (backend rejects them on PATCH), so the edit dialog shows them
 * read-only. Opening balance is entered as a positive rupee magnitude; for a
 * credit card it's the amount *owed* and stored negative (the backend's debt
 * sign convention) — the field label carries that, so there's no sign surprise.
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { IconArchive, IconPlus } from "@/components/icons";
import { Field, PickerButton, TextInput } from "@/components/form/fields";
import {
  ApiError,
  createAccount,
  deleteAccount,
  listAccounts,
  patchAccount,
  type AccountCreate,
  type AccountRead,
  type AccountUpdate,
} from "@/lib/api/client";
import { formatMoney, rupeesToPaise } from "@/lib/format";
import { Sensitive } from "@/components/balance-visibility";
import { cn } from "@/lib/utils";

type AccountType = AccountRead["type"];
type Currency = AccountRead["currency"];

const TYPE_LABELS: Record<AccountType, string> = {
  credit_card: "Credit card",
  bank: "Bank",
  cash: "Cash",
  investment: "Investment",
};

// Spending is INR-only in v1 (PRD §Working agreements): the dashboard net-worth
// sums account balances as raw paise with no per-account FX, so a USD account
// would mis-state net worth. USD returns here with the currency-aware spend
// rollup (deferred multi-currency-spend). The `Currency` type still allows USD
// for the currency-aware investment side.
const CURRENCIES: Currency[] = ["INR"];

// Mirror of backend SUPPORTED_CC_ISSUERS (app/services/import_service.py). Only
// issuers with a registered CC statement parser belong here — a credit card with
// any other issuer would crash at statement upload (ParserNotRegisteredError), so
// the issuer picker below is constrained to this list. Keep in sync by hand.
const CC_ISSUERS = [
  { value: "axis", label: "Axis" },
  { value: "icici", label: "ICICI" },
] as const;

// Issuers are stored lowercase (the parser-dispatch key). For display, map a
// known CC issuer to its brand-cased label; fall back to the raw value for
// free-text issuers (e.g. "hdfc") where we have no canonical casing.
function issuerLabel(issuer: string): string {
  return CC_ISSUERS.find((i) => i.value === issuer)?.label ?? issuer;
}

/** The one open dialog (or none). `edit`/`archive` always carry their row. */
type AccountDialog =
  | null
  | { kind: "create" }
  | { kind: "edit"; account: AccountRead }
  | { kind: "archive"; account: AccountRead };

export function AccountsManager() {
  const accountsQuery = useQuery({
    queryKey: ["accounts"],
    queryFn: listAccounts,
  });
  const accounts = accountsQuery.data ?? [];

  // One dialog at a time: a create form, an edit form for a row, or an archive
  // confirm for a row. A single discriminated state makes the "edit/archive
  // without a row" combination unrepresentable.
  const [dialog, setDialog] = useState<AccountDialog>(null);
  const closeDialog = () => setDialog(null);

  return (
    <Card className="max-w-3xl">
      <CardHeader className="flex flex-row items-center justify-between border-b">
        <CardTitle className="text-[14px]">
          {accounts.length} {accounts.length === 1 ? "account" : "accounts"}
        </CardTitle>
        <Button
          type="button"
          onClick={() => setDialog({ kind: "create" })}
          className="h-8 gap-1.5 px-2.5 text-[12px] font-medium"
        >
          <IconPlus className="size-3" />
          New account
        </Button>
      </CardHeader>

      <CardContent className="px-0">
        {accountsQuery.isPending ? (
          <Row tone="muted">Loading…</Row>
        ) : accountsQuery.isError ? (
          <Row tone="error">Couldn’t load accounts — is the API running?</Row>
        ) : accounts.length === 0 ? (
          <Row tone="muted">
            No accounts yet. Create one to start importing.
          </Row>
        ) : (
          accounts.map((a) => (
            <div
              key={a.id}
              className="flex items-center gap-3 border-b border-border/60 px-4 py-2.5 last:border-b-0"
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate text-[13px] font-medium text-foreground">
                    {a.name}
                  </span>
                  {a.last4 ? (
                    <span className="text-[11px] tabular-nums text-muted-foreground/70">
                      ··{a.last4}
                    </span>
                  ) : null}
                </div>
                <div className="mt-0.5 flex items-center gap-2 text-[11.5px] text-muted-foreground">
                  <span>{TYPE_LABELS[a.type]}</span>
                  <span aria-hidden>·</span>
                  <span className="tabular-nums">
                    <Sensitive>
                      {formatMoney(a.opening_balance_paise, a.currency)}
                    </Sensitive>
                  </span>
                  {a.issuer ? (
                    <>
                      <span aria-hidden>·</span>
                      <span>{issuerLabel(a.issuer)}</span>
                    </>
                  ) : null}
                </div>
              </div>
              <Button
                type="button"
                variant="ghost"
                onClick={() => setDialog({ kind: "edit", account: a })}
                aria-label={`Edit ${a.name}`}
                className="h-7 px-2.5 text-[12px]"
              >
                Edit
              </Button>
              <Button
                type="button"
                variant="ghost"
                onClick={() => setDialog({ kind: "archive", account: a })}
                className="h-7 gap-1 px-2 text-[12px] text-muted-foreground hover:text-neg"
                aria-label={`Archive ${a.name}`}
                title="Archive"
              >
                <IconArchive className="size-3.5" />
              </Button>
            </div>
          ))
        )}
      </CardContent>

      {dialog?.kind === "create" ? (
        <AccountFormDialog mode="create" onClose={closeDialog} />
      ) : dialog?.kind === "edit" ? (
        <AccountFormDialog
          key={dialog.account.id}
          mode="edit"
          account={dialog.account}
          accounts={accounts}
          onClose={closeDialog}
        />
      ) : dialog?.kind === "archive" ? (
        <ArchiveConfirm account={dialog.account} onClose={closeDialog} />
      ) : null}
    </Card>
  );
}

function Row({
  children,
  tone,
}: {
  children: React.ReactNode;
  tone: "muted" | "error";
}) {
  return (
    <p
      className={cn(
        "px-4 py-8 text-center text-[13px]",
        tone === "error" ? "text-neg" : "text-muted-foreground",
      )}
    >
      {children}
    </p>
  );
}

type AccountFormDialogProps =
  | { mode: "create"; onClose: () => void }
  // `accounts` supplies the parent picker's options (bank accounts). Edit-only:
  // `parent_account_id` is PATCH-only, since the backend's link gate validates
  // against the stored `type`.
  | {
      mode: "edit";
      account: AccountRead;
      accounts: AccountRead[];
      onClose: () => void;
    };

function AccountFormDialog(props: AccountFormDialogProps) {
  const { onClose } = props;
  // Present only in edit mode. Narrowed `props.account` is used wherever a
  // definite value is required (no non-null assertions).
  const account = props.mode === "edit" ? props.account : undefined;
  const queryClient = useQueryClient();
  const [name, setName] = useState(account?.name ?? "");
  const [type, setType] = useState<AccountType>(account?.type ?? "credit_card");
  const [currency, setCurrency] = useState<Currency>(
    account?.currency ?? "INR",
  );
  const [issuer, setIssuer] = useState(account?.issuer ?? "");
  const [last4, setLast4] = useState(account?.last4 ?? "");
  const [balance, setBalance] = useState(""); // create-only; rupee magnitude
  const [parentId, setParentId] = useState<number | null>(
    account?.parent_account_id ?? null,
  );

  const isCredit = type === "credit_card";
  // An investment account is a placeholder — the backend 422s any non-zero
  // opening balance (PRD §F6), because its value is already counted as holdings.
  // Hiding the field is what keeps the only UI that can create an account from
  // walking into a guaranteed rejection; the 422 stays as the boundary guard.
  const isInvestment = type === "investment";

  const mutation = useMutation({
    mutationFn: () => {
      if (props.mode === "create") {
        // Positive rupee magnitude → paise; a credit card's balance is debt,
        // stored negative (backend's sign rule). The explicit 0 for investment
        // is load-bearing, not cosmetic: `balance` survives a type switch (bank
        // → investment) with a typed value still in it, and the field is only
        // hidden, not cleared.
        const magnitude = rupeesToPaise(balance);
        const body: AccountCreate = {
          name: name.trim(),
          type,
          currency,
          issuer: issuer.trim() || null,
          last4: last4.trim() || null,
          opening_balance_paise: isInvestment
            ? 0
            : isCredit
              ? -magnitude
              : magnitude,
        };
        return createAccount(body);
      }
      // Edit: only name/issuer/last4/parent_account_id are mutable; send only
      // what changed.
      const body: AccountUpdate = {};
      if (name.trim() !== props.account.name) body.name = name.trim();
      const nextIssuer = issuer.trim() || null;
      if (nextIssuer !== (props.account.issuer ?? null))
        body.issuer = nextIssuer;
      const nextLast4 = last4.trim() || null;
      if (nextLast4 !== (props.account.last4 ?? null)) body.last4 = nextLast4;
      // An explicit `null` unlinks (the backend's documented semantics), so this
      // must go through the changed-fields diff rather than being sent always —
      // omitted means "leave the link alone".
      if (parentId !== props.account.parent_account_id)
        body.parent_account_id = parentId;
      return patchAccount(props.account.id, body);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["accounts"] });
      // Net worth / account rows on /dashboard read the same accounts (PRD §F9);
      // nudge the dashboards query so a create/edit/archive shows there too.
      queryClient.invalidateQueries({ queryKey: ["dashboards"] });
      onClose();
    },
  });

  const canSubmit =
    name.trim().length > 0 &&
    // A credit card must carry a supported issuer (the backend rejects anything
    // else with 422); other types leave issuer optional.
    (!isCredit || CC_ISSUERS.some((i) => i.value === issuer)) &&
    !mutation.isPending;

  // Only bank accounts can parent a card (backend rule 4). The list comes from
  // GET /accounts, which already filters archived rows.
  const parentOptions =
    props.mode === "edit"
      ? props.accounts.filter((a) => a.type === "bank")
      : [];
  // A parent archived *after* linking is absent from the options above, and
  // archiving deliberately does NOT unlink. Naming it keeps the dialog from
  // rendering "None" over a link that is still live — which would read as
  // unlinked and make an unrelated save look like it had unlinked it.
  const parentLabel =
    parentId === null
      ? "None"
      : (parentOptions.find((a) => a.id === parentId)?.name ??
        `Archived account (#${parentId})`);

  return (
    <Dialog open onOpenChange={(open) => (open ? null : onClose())}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {props.mode === "create" ? "New account" : "Edit account"}
          </DialogTitle>
          {props.mode === "edit" ? (
            <DialogDescription>
              Type, currency, and opening balance are fixed once an account is
              created.
            </DialogDescription>
          ) : (
            <DialogDescription>
              Type, currency, and opening balance are set now and can’t be
              changed later.
            </DialogDescription>
          )}
        </DialogHeader>

        <div className="grid gap-3">
          <Field label="Name">
            <TextInput
              value={name}
              onChange={setName}
              placeholder="e.g. Axis Flipkart"
              autoFocus
            />
          </Field>

          {props.mode === "create" ? (
            <>
              <Field label="Type">
                <PickerButton label={TYPE_LABELS[type]}>
                  {(["credit_card", "bank", "cash", "investment"] as const).map(
                    (t) => (
                      <DropdownMenuItem key={t} onSelect={() => setType(t)}>
                        {TYPE_LABELS[t]}
                      </DropdownMenuItem>
                    ),
                  )}
                </PickerButton>
              </Field>

              <Field label="Currency">
                <PickerButton label={currency}>
                  {CURRENCIES.map((c) => (
                    <DropdownMenuItem key={c} onSelect={() => setCurrency(c)}>
                      {c}
                    </DropdownMenuItem>
                  ))}
                </PickerButton>
              </Field>
            </>
          ) : (
            <div className="grid grid-cols-2 gap-3">
              <ReadOnlyField
                label="Type"
                value={TYPE_LABELS[props.account.type]}
              />
              <ReadOnlyField label="Currency" value={props.account.currency} />
            </div>
          )}

          <div className="grid grid-cols-2 gap-3">
            {isCredit ? (
              // Credit-card issuer selects the statement parser, so it's a
              // required pick from the issuers we actually parse — not free text.
              <Field label="Issuer">
                <PickerButton
                  label={
                    CC_ISSUERS.find((i) => i.value === issuer)?.label ??
                    "Select issuer…"
                  }
                  muted={!CC_ISSUERS.some((i) => i.value === issuer)}
                >
                  {CC_ISSUERS.map((i) => (
                    <DropdownMenuItem
                      key={i.value}
                      onSelect={() => setIssuer(i.value)}
                    >
                      {i.label}
                    </DropdownMenuItem>
                  ))}
                </PickerButton>
              </Field>
            ) : (
              <Field label="Issuer (optional)">
                <TextInput
                  value={issuer}
                  onChange={setIssuer}
                  placeholder="e.g. hdfc"
                />
              </Field>
            )}
            <Field label="Last 4 (optional)">
              <TextInput
                value={last4}
                onChange={(v) => setLast4(v.replace(/\D/g, "").slice(0, 4))}
                placeholder="1234"
                inputMode="numeric"
              />
            </Field>
          </div>

          {/* F4a rule 1: a CC bill payment is only reclassified from income to
              a transfer once the card is linked to the bank it's paid from
              (PRD §F4a). Credit cards only — the backend 422s a parent on any
              other type. */}
          {props.mode === "edit" && props.account.type === "credit_card" ? (
            <Field label="Paid from (for bill matching)">
              <PickerButton label={parentLabel} muted={parentId === null}>
                <DropdownMenuItem onSelect={() => setParentId(null)}>
                  None
                </DropdownMenuItem>
                {parentOptions.map((a) => (
                  <DropdownMenuItem
                    key={a.id}
                    onSelect={() => setParentId(a.id)}
                  >
                    {a.name}
                  </DropdownMenuItem>
                ))}
              </PickerButton>
            </Field>
          ) : null}

          {props.mode === "create" && isInvestment ? (
            <p className="text-[12px] text-muted-foreground">
              Investment accounts don’t hold a balance — they group a broker, and
              the value comes from your holdings. Add PPF, EPF or anything else
              not yet itemised on the Investments page instead.
            </p>
          ) : props.mode === "create" ? (
            <Field label={isCredit ? "Balance owed" : "Opening balance"}>
              <TextInput
                value={balance}
                onChange={setBalance}
                placeholder="0.00"
                inputMode="decimal"
                type="number"
                step="0.01"
                min="0"
              />
            </Field>
          ) : (
            <ReadOnlyField
              label="Opening balance"
              value={
                <Sensitive>
                  {formatMoney(
                    props.account.opening_balance_paise,
                    props.account.currency,
                  )}
                </Sensitive>
              }
            />
          )}
        </div>

        {mutation.isError ? (
          <p className="text-[12px] text-neg">
            {mutation.error instanceof ApiError
              ? mutation.error.detail
              : "Couldn’t save — try again."}
          </p>
        ) : null}

        <DialogFooter>
          <Button
            variant="ghost"
            className="h-8 px-3 text-[12.5px]"
            onClick={onClose}
            disabled={mutation.isPending}
          >
            Cancel
          </Button>
          <Button
            className="h-8 px-3 text-[12.5px]"
            onClick={() => mutation.mutate()}
            disabled={!canSubmit}
          >
            {mutation.isPending
              ? "Saving…"
              : props.mode === "create"
                ? "Create"
                : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ArchiveConfirm({
  account,
  onClose,
}: {
  account: AccountRead;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => deleteAccount(account.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["accounts"] });
      // Archiving does NOT drop the account from net worth — the overview
      // deliberately omits an archived_at filter so the figure can't silently
      // change on archive. It DOES move the row to the "archived" presentation,
      // which /dashboard reads off the same aggregate (PRD §F9).
      queryClient.invalidateQueries({ queryKey: ["dashboards"] });
      onClose();
    },
  });

  return (
    <Dialog open onOpenChange={(open) => (open ? null : onClose())}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>Archive {account.name}?</DialogTitle>
          <DialogDescription>
            It’s removed from pickers and lists. Existing transactions keep this
            account, and the name becomes available again. This can’t be undone
            — there’s no un-archive.
          </DialogDescription>
        </DialogHeader>
        {mutation.isError ? (
          <p className="text-[12px] text-neg">
            {mutation.error instanceof ApiError
              ? mutation.error.detail
              : "Couldn’t archive — try again."}
          </p>
        ) : null}
        <DialogFooter>
          <Button
            variant="ghost"
            className="h-8 px-3 text-[12.5px]"
            onClick={onClose}
            disabled={mutation.isPending}
          >
            Cancel
          </Button>
          <Button
            variant="destructive"
            className="h-8 px-3 text-[12.5px]"
            onClick={() => mutation.mutate()}
            disabled={mutation.isPending}
          >
            {mutation.isPending ? "Archiving…" : "Archive"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ReadOnlyField({
  label,
  value,
}: {
  label: string;
  value: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <span
        className="text-[10.5px] font-medium uppercase text-muted-foreground"
        style={{ letterSpacing: "0.08em" }}
      >
        {label}
      </span>
      <span className="text-[12.5px] tabular-nums text-foreground/70">
        {value}
      </span>
    </div>
  );
}
