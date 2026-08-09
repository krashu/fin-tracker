"use client";

/**
 * Registration page (PRD §Users & access v2). Public, rendered bare by the route
 * guard. Open self-service signup → a fresh, provisioned-but-empty workspace with
 * default categories (backend seeds them). Redirects to /dashboard on success.
 */
import { useEffect, useState, type FormEvent } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { useAuth } from "@/components/auth/auth-provider";
import { AuthShell } from "@/components/auth/auth-shell";
import { Field, TextInput } from "@/components/form/fields";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api/client";

// Stable target for the credential fields' aria-describedby. Deliberately NOT
// paired with aria-invalid: the backend returns a detail string, not a field, so
// an "email already registered" can't be attributed to an input without
// asserting something the app doesn't know. Display name is left undescribed —
// it is optional and never the subject of a server error.
const ERROR_ID = "register-error";

export default function RegisterPage() {
  const router = useRouter();
  const { status, register } = useAuth();
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // Name the client gate that's holding the submit disabled, rather than
  // leaving the button inert with no stated reason. Not on a pristine form: an
  // error-coloured line on first paint reads as a failure, and this is a
  // public, first-contact surface. The client hint outranks the API error,
  // mirroring security-manager.tsx:107-127.
  const started = email !== "" || password !== "";
  const hint = !started
    ? null
    : email === ""
      ? "Enter your email address."
      : password.length < 8
        ? "Password must be at least 8 characters."
        : null;
  const message = hint ?? error;

  useEffect(() => {
    if (status === "authenticated") router.replace("/dashboard");
  }, [status, router]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await register(email, password, displayName);
      router.replace("/dashboard");
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.detail
          : "Something went wrong — try again.",
      );
      setSubmitting(false);
    }
  }

  return (
    <AuthShell
      title="Create your account"
      subtitle="Start tracking your finances in a private workspace."
      footer={
        <>
          Already have an account?{" "}
          <Link href="/login" className="text-foreground hover:underline">
            Sign in
          </Link>
        </>
      }
    >
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <Field label="Email">
          <TextInput
            type="email"
            value={email}
            onChange={setEmail}
            autoComplete="email"
            placeholder="you@example.com"
            aria-describedby={message ? ERROR_ID : undefined}
            required
          />
        </Field>
        <Field label="Display name (optional)">
          <TextInput
            type="text"
            value={displayName}
            onChange={setDisplayName}
            autoComplete="name"
            placeholder="e.g. Alex"
            maxLength={128}
          />
        </Field>
        <Field label="Password">
          <TextInput
            type="password"
            value={password}
            onChange={setPassword}
            autoComplete="new-password"
            placeholder="At least 8 characters"
            minLength={8}
            aria-describedby={message ? ERROR_ID : undefined}
            required
          />
        </Field>

        {message ? (
          <p id={ERROR_ID} role="alert" className="text-[12px] text-neg">
            {message}
          </p>
        ) : null}

        <Button
          type="submit"
          size="lg"
          className="mt-1 w-full"
          disabled={submitting || email === "" || password.length < 8}
        >
          {submitting ? "Creating account…" : "Create account"}
        </Button>
      </form>
    </AuthShell>
  );
}
