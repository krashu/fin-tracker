"use client";

/**
 * "Delete my account" card (ADR-0003 rule 13b). Erases the signed-in account and everything it
 * owns at once, so the destructive button stays disabled until the password is typed AND the
 * confirmation phrase matches exactly (no trim, no case folding: the backend compares exactly).
 * A backup download sits above the form, since the erase has no undo.
 *
 * Renders from `user.can_manage_credentials` (server-computed): the demo account and guests
 * get a notice instead of a form that could only 403. Nothing renders until the user is known. A wrong password is a 400 body error,
 * so the session is kept and the reason shown; on success the provider drops the session and
 * we go to /login, as logout does.
 */
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { useRouter } from "next/navigation";

import { useAuth } from "@/components/auth/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, TextInput } from "@/components/form/fields";
import {
  ApiError,
  DELETE_ACCOUNT_PHRASE,
  downloadBackup,
} from "@/lib/api/client";
import { saveBlob } from "@/lib/download";

export function DeleteAccountCard() {
  const { user, deleteAccount } = useAuth();
  const router = useRouter();
  const [password, setPassword] = useState("");
  const [phrase, setPhrase] = useState("");

  const backup = useMutation({
    mutationFn: downloadBackup,
    onSuccess: ({ blob, filename }) => saveBlob(blob, filename),
  });

  const erase = useMutation({
    mutationFn: () =>
      deleteAccount({ password, confirmation: DELETE_ACCOUNT_PHRASE }),
    onSuccess: () => router.replace("/login"),
  });

  // Until /auth/me lands there is nothing to decide: render neither the form nor the notice.
  if (!user) return null;
  if (!user.can_manage_credentials) {
    return (
      <Card className="max-w-xl">
        <CardHeader>
          <CardTitle>Delete my account</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-[12.5px] text-muted-foreground">
            The demo account and guest sandboxes can&apos;t be deleted here. A
            guest sandbox is removed when it expires.
          </p>
        </CardContent>
      </Card>
    );
  }

  const canSubmit =
    password !== "" && phrase === DELETE_ACCOUNT_PHRASE && !erase.isPending;

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    erase.mutate();
  }

  return (
    <Card className="max-w-xl">
      <CardHeader>
        <CardTitle>Delete my account</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <p className="text-[12.5px] text-muted-foreground">
          This erases your accounts, transactions, categories, rules, tags and
          investments at once. There is no undo.
        </p>

        <div className="flex flex-col gap-2">
          <p className="text-[12.5px] text-muted-foreground">
            Download a backup first. It holds your confirmed transactions and
            the accounts and categories they use; investments are not in it.
          </p>
          <div>
            <Button
              type="button"
              variant="outline"
              disabled={backup.isPending}
              onClick={() => backup.mutate()}
              className="h-9 px-3.5 text-[12.5px] font-medium"
            >
              {backup.isPending ? "Preparing…" : "Download backup"}
            </Button>
          </div>
          {backup.isError ? (
            <p className="text-[12px] text-neg">
              {backup.error instanceof ApiError
                ? backup.error.detail
                : "Couldn’t download the backup — try again."}
            </p>
          ) : null}
        </div>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          {/* sr-only username hint for password managers, as in SecurityManager. */}
          <input
            type="text"
            autoComplete="username"
            value={user.email ?? ""}
            readOnly
            tabIndex={-1}
            aria-hidden="true"
            className="sr-only"
          />
          <Field label="Current password">
            <TextInput
              type="password"
              value={password}
              onChange={setPassword}
              autoComplete="current-password"
              placeholder="••••••••"
              required
            />
          </Field>
          <Field label={`Type "${DELETE_ACCOUNT_PHRASE}" to confirm`}>
            <TextInput
              value={phrase}
              onChange={setPhrase}
              autoComplete="off"
              required
            />
          </Field>

          {erase.isError ? (
            <p className="text-[12px] text-neg">
              {erase.error instanceof ApiError
                ? erase.error.detail
                : "Couldn’t delete your account — try again."}
            </p>
          ) : null}

          <div>
            <Button
              type="submit"
              variant="destructive"
              disabled={!canSubmit}
              className="h-9 px-3.5 text-[12.5px] font-medium"
            >
              {erase.isPending ? "Deleting…" : "Delete my account"}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
