"use client";

/**
 * Statement upload (PRD §F1). Pick an account, choose a PDF, optionally supply a
 * password for protected statements → POST /imports. Routing is driven by the
 * response's `pending_count` (rows on the batch still awaiting review), NOT by
 * `already_imported`: >0 → open the review queue; 0 → nothing to review, so we
 * stay and show a message. A re-upload reconciles the file against expenses and
 * re-stages rows missing from the board (discarded / partially-cancelled), so a
 * re-upload with something to review routes to the queue just like a fresh one.
 * Because re-upload re-parses, a protected PDF needs its password again.
 */
import { useId, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { IconChevronDown, IconDoc, IconUpload } from "@/components/icons";
import { Field, usePickerLabelledBy } from "@/components/form/fields";
import {
  ApiError,
  createImport,
  listAccounts,
  type AccountRead,
  type ImportSummary,
} from "@/lib/api/client";
import { accountLabel } from "@/lib/accounts";
import { cn } from "@/lib/utils";

export function UploadForm() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [accountId, setAccountId] = useState<number | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [password, setPassword] = useState("");
  const [summary, setSummary] = useState<ImportSummary | null>(null);
  // Set when the backend found this exact file already imported into a DIFFERENT
  // account (UX-09b). Holds the routing target so "Review anyway" can still get
  // there — auto-navigating would bury the warning behind the queue.
  const [wrongAccount, setWrongAccount] = useState<ImportSummary | null>(null);

  const accountsQuery = useQuery({
    queryKey: ["accounts"],
    queryFn: listAccounts,
  });
  // Never offer an account the backend will reject. Statement parsing dispatches on
  // (issuer, type) and every registered parser is a credit_card one, so a bank / cash /
  // investment pick posts the whole file and comes back with a 422 that names neither
  // the account nor the type — nothing points at the picker as the cause. The two
  // /expenses pickers filter for the same reason.
  const accounts = (accountsQuery.data ?? []).filter(
    (a) => a.type === "credit_card",
  );
  const selectedAccount = accounts.find((a) => a.id === accountId) ?? null;

  const mutation = useMutation({
    mutationFn: (vars: { accountId: number; file: File; password?: string }) =>
      createImport({
        account_id: vars.accountId,
        file: vars.file,
        password: vars.password,
      }),
    onSuccess: (s) => {
      // An import may create pending rows → refresh the top-bar bell.
      queryClient.invalidateQueries({ queryKey: ["imports", "pending"] });
      // Route on pending_count, not already_imported: anything to review (fresh
      // rows OR rows resurfaced by a re-upload) opens the queue; zero pending
      // means everything's already in expenses, so stay and show the message.
      // Carry the already-present (skipped) count so the queue can note that the
      // file's other rows are already imported and aren't shown there.
      // ...unless this exact file was already imported into a DIFFERENT account
      // (UX-09b): stop and say so first. The rows ARE staged by now, so the honest
      // framing is "this may be the wrong account — you can still cancel", and
      // cancelling from the queue is the documented way out.
      if (s.duplicate_of_account_id != null && s.pending_count > 0) {
        setWrongAccount(s);
        return;
      }
      if (s.pending_count > 0) {
        const qs = s.skipped > 0 ? `?present=${s.skipped}` : "";
        router.push(`/imports/review/${s.batch_id}${qs}`);
      } else setSummary(s);
    },
  });

  function handleUpload() {
    if (accountId == null || file == null) return;
    setSummary(null);
    setWrongAccount(null);
    mutation.mutate({ accountId, file, password: password || undefined });
  }

  const canSubmit = accountId != null && file != null && !mutation.isPending;

  return (
    <form
      className="flex max-w-xl flex-col gap-4"
      onSubmit={(e) => {
        e.preventDefault();
        handleUpload();
      }}
    >
      <Field label="Account">
        <AccountPicker
          accounts={accounts}
          accountId={accountId}
          selectedAccount={selectedAccount}
          onSelect={setAccountId}
        />
      </Field>

      {accountsQuery.isSuccess && accounts.length === 0 ? (
        <p className="text-[11.5px] text-muted-foreground">
          Statement import needs a credit-card account —{" "}
          <Link
            href="/settings/accounts"
            className="font-medium text-primary hover:underline"
          >
            add one first
          </Link>
          .
        </p>
      ) : null}

      <Field label="Statement PDF">
        {/* sr-only (not hidden) keeps the file input in the tab order + a11y tree;
            the visible label is the button and shows the focus ring via has-[]. */}
        <label className="flex cursor-pointer items-center gap-2 rounded-md border border-border bg-background px-2.5 py-2 text-[12.5px] transition-colors hover:bg-muted/50 has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-ring">
          <input
            type="file"
            accept="application/pdf"
            className="sr-only"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
          <IconDoc className="size-3.5 shrink-0 text-muted-foreground" />
          <span
            className={cn(
              "truncate",
              file ? "text-foreground" : "text-muted-foreground",
            )}
          >
            {file ? file.name : "Choose a PDF…"}
          </span>
        </label>
      </Field>

      <Field label="Password">
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Only if the PDF is protected"
          className="w-full rounded-md border border-border bg-background px-2.5 py-2 text-[12.5px] text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      </Field>

      {mutation.isError ? (
        <p className="text-[12px] text-neg">
          {mutation.error instanceof ApiError
            ? mutation.error.detail
            : "Couldn’t upload — try again."}
        </p>
      ) : null}

      {/* UX-09b: this exact file is already imported under another account, so the
          picked account is probably wrong. Deliberately blocks the auto-navigate
          rather than showing a banner inside the queue — and offers the way through,
          because a same-file-different-account import is legitimate for a joint
          statement. The other account is named from the ["accounts"] cache; an
          archived one isn't in that list (GET /accounts filters it), which is what
          `duplicate_of_account_archived` is for. */}
      {wrongAccount ? (
        <div className="rounded-md border border-warn-soft-border bg-warn-soft-bg px-3 py-2.5 text-[12.5px]">
          <p className="font-medium text-foreground">
            You already imported this file into{" "}
            {duplicateAccountLabel(
              accountsQuery.data ?? [],
              wrongAccount.duplicate_of_account_id,
              wrongAccount.duplicate_of_account_archived,
            )}
            .
          </p>
          <p className="mt-1 text-muted-foreground">
            Its {wrongAccount.pending_count}{" "}
            {wrongAccount.pending_count === 1 ? "row is" : "rows are"} staged
            against{" "}
            {selectedAccount ? accountLabel(selectedAccount) : "this account"} —
            nothing is on your board yet, so cancel the import from the queue if
            you picked the wrong account.
          </p>
          <Link
            href={`/imports/review/${wrongAccount.batch_id}${
              wrongAccount.skipped > 0 ? `?present=${wrongAccount.skipped}` : ""
            }`}
            className="mt-1.5 inline-block font-medium text-primary hover:underline"
          >
            Review anyway
          </Link>
        </div>
      ) : null}

      {/* Reached only when pending_count === 0: every parsed row is already in
          expenses, so there is nothing to review. Wording distinguishes a
          re-upload from a fresh upload whose rows all deduped away. */}
      {summary ? (
        <div className="rounded-md border border-border bg-muted/40 px-3 py-2.5 text-[12.5px]">
          <p className="text-foreground">
            {summary.already_imported
              ? "This statement was already imported."
              : "Nothing new to import from this statement."}
          </p>
          <p className="mt-1 text-muted-foreground">
            All of its transactions are already in your{" "}
            <Link
              href="/expenses"
              className="font-medium text-primary hover:underline"
            >
              expenses
            </Link>
            .
          </p>
        </div>
      ) : null}

      <div>
        <Button
          type="submit"
          disabled={!canSubmit}
          className="h-9 gap-1.5 px-3.5 text-[12.5px] font-medium"
        >
          <IconUpload className="size-3.5" />
          {mutation.isPending ? "Uploading…" : "Upload statement"}
        </Button>
      </div>
    </form>
  );
}

/** Name the account a duplicate file came from. The backend returns an id, never a
 * name, so it's resolved against the ["accounts"] cache — which omits archived
 * accounts, hence the flag-driven fallback. "another account" covers the remaining
 * case (a cache that hasn't loaded, or an id the list doesn't carry). */
function duplicateAccountLabel(
  accounts: AccountRead[],
  accountId: number | null,
  archived: boolean,
): string {
  const account = accounts.find((a) => a.id === accountId);
  if (account) return accountLabel(account);
  return archived ? "an archived account" : "another account";
}

/**
 * The account picker. Its own component (rather than inline JSX) so it renders
 * *inside* the Field and can therefore read the Field's caption id — a hook in
 * UploadForm sits above the provider and would only ever see undefined.
 *
 * Deliberately not `PickerButton`: this menu doesn't width-match its trigger.
 */
function AccountPicker({
  accounts,
  accountId,
  selectedAccount,
  onSelect,
}: {
  accounts: AccountRead[];
  accountId: number | null;
  selectedAccount: AccountRead | null;
  onSelect: (id: number) => void;
}) {
  const valueId = useId();
  const labelledBy = usePickerLabelledBy(valueId);
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="outline"
          aria-labelledby={labelledBy}
          className="h-9 w-full justify-between px-2.5 text-[12.5px] font-normal"
        >
          <span
            id={valueId}
            className={cn(accountId == null && "text-muted-foreground")}
          >
            {selectedAccount
              ? accountLabel(selectedAccount)
              : "Select an account"}
          </span>
          <IconChevronDown className="size-3 text-muted-foreground" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="max-h-72">
        {accounts.length === 0 ? (
          <DropdownMenuItem disabled>
            <span className="text-muted-foreground">
              No credit-card accounts
            </span>
          </DropdownMenuItem>
        ) : (
          accounts.map((a) => (
            <DropdownMenuItem key={a.id} onSelect={() => onSelect(a.id)}>
              {accountLabel(a)}
            </DropdownMenuItem>
          ))
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
