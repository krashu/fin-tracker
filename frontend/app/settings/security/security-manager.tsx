"use client";

/**
 * Change-password form (PRD §Users & access v2). Verifies the current password,
 * sets a new one, and — server-side — revokes every OTHER device's refresh session
 * while keeping this device signed in on freshly-issued cookies. No query
 * invalidation: the identity is unchanged and the cookie swap is transparent.
 *
 * Known narrow race (accepted, not coordinated): if another tab in THIS browser has
 * a request in flight during the change, it can 401 → silent-refresh with the
 * now-revoked cookie → `onAuthFailure` bounces the whole browser to /login before
 * the fresh Set-Cookie lands. Low probability for single-user self-host; the cost is
 * one re-login, cheaper than serializing the app around the mutation.
 */
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";

import { useAuth } from "@/components/auth/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, TextInput } from "@/components/form/fields";
import { ApiError, changePassword } from "@/lib/api/client";

const MIN_LENGTH = 8;

export function SecurityManager() {
  const { user } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [done, setDone] = useState(false);

  const mutation = useMutation({
    mutationFn: () => changePassword(current, next),
    onSuccess: () => {
      setDone(true);
      setCurrent("");
      setNext("");
      setConfirm("");
    },
  });

  // Client-side guards mirrored by the backend (length + "must differ"); keep the
  // submit disabled until they hold so we never fire a request we know will 400/422.
  const tooShort = next.length > 0 && next.length < MIN_LENGTH;
  const mismatch = confirm.length > 0 && next !== confirm;
  const sameAsCurrent = next.length > 0 && next === current;
  const canSubmit =
    current !== "" &&
    next.length >= MIN_LENGTH &&
    next === confirm &&
    next !== current &&
    !mutation.isPending;

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setDone(false);
    mutation.mutate();
  }

  return (
    <Card className="max-w-xl">
      <CardHeader>
        <CardTitle>Change password</CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          {/* A password manager needs a username in the SAME form to know this
              is an UPDATE for an existing login rather than a new credential —
              without it Chrome logs "Password forms should have (optionally
              hidden) username fields" and managers offer "save" instead of
              "update". sr-only, NOT hidden/display:none: managers skip fields
              they compute as invisible, and sr-only only clips them. It is a
              hint for the manager, not a control — readOnly because the account
              isn't editable here, tabIndex/aria-hidden because a name the user
              can neither change nor act on is noise in the tab order and in the
              a11y tree. Not focusable by tab, so aria-hidden is legitimate. */}
          <input
            type="text"
            autoComplete="username"
            value={user?.email ?? ""}
            readOnly
            tabIndex={-1}
            aria-hidden="true"
            className="sr-only"
          />
          <Field label="Current password">
            <TextInput
              type="password"
              value={current}
              onChange={(v) => {
                setCurrent(v);
                setDone(false);
              }}
              autoComplete="current-password"
              placeholder="••••••••"
              required
            />
          </Field>
          <Field label="New password">
            <TextInput
              type="password"
              value={next}
              onChange={(v) => {
                setNext(v);
                setDone(false);
              }}
              autoComplete="new-password"
              placeholder="At least 8 characters"
              required
            />
          </Field>
          <Field label="Confirm new password">
            <TextInput
              type="password"
              value={confirm}
              onChange={(v) => {
                setConfirm(v);
                setDone(false);
              }}
              autoComplete="new-password"
              placeholder="Re-enter the new password"
              required
            />
          </Field>

          {tooShort ? (
            <p className="text-[12px] text-neg">
              New password must be at least {MIN_LENGTH} characters.
            </p>
          ) : sameAsCurrent ? (
            <p className="text-[12px] text-neg">
              New password must differ from the current one.
            </p>
          ) : mismatch ? (
            <p className="text-[12px] text-neg">Passwords don’t match.</p>
          ) : mutation.isError ? (
            <p className="text-[12px] text-neg">
              {mutation.error instanceof ApiError
                ? mutation.error.detail
                : "Couldn’t change your password — try again."}
            </p>
          ) : done ? (
            <p className="text-[12px] text-pos">
              Password changed. Your other devices are being signed out.
            </p>
          ) : null}

          <div>
            <Button
              type="submit"
              disabled={!canSubmit}
              className="h-9 gap-1.5 px-3.5 text-[12.5px] font-medium"
            >
              {mutation.isPending ? "Changing…" : "Change password"}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
