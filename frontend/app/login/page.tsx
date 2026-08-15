"use client";

/**
 * Sign-in page (PRD §Users & access v2). Public, rendered bare by the route
 * guard. Email + password → cookie session; a "Try the demo" button logs into the
 * shared demo account. On success — and immediately if the visitor is already
 * authenticated — redirects to the validated `?next` destination the route guard
 * attached, falling back to /dashboard.
 */
import { useEffect, useState, type FormEvent } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";

import { useAuth } from "@/components/auth/auth-provider";
import { IDLE_LOGOUT_FLAG } from "@/components/auth/use-idle-logout";
import { AuthShell } from "@/components/auth/auth-shell";
import { Field, TextInput } from "@/components/form/fields";
import { Button } from "@/components/ui/button";
import { ApiError, getAuthConfig } from "@/lib/api/client";

// Stable target for the fields' aria-describedby. Deliberately NOT paired with
// aria-invalid: the backend returns a detail string, not a field, so "wrong
// password" can't be attributed to the email input without asserting something
// the app doesn't know.
const ERROR_ID = "login-error";

const DEFAULT_DESTINATION = "/dashboard";

/**
 * Resolve the post-login destination from `?next`, which RouteGuard attaches when
 * it bounces an unauthenticated visitor off a protected route.
 *
 * /login is a public front door, so `next` is attacker-controlled: a crafted link
 * that renders the real sign-in form and then lands the user somewhere else is a
 * credential-phishing vector. Validation is therefore done by *parsing*, not by
 * string rules — a rule list ("starts with /, not //, no :") is bypassable,
 * because `?next=/%09/evil.example` passes every one of those rules and the WHATWG
 * URL parser then strips the tab and resolves the result as protocol-relative
 * `//evil.example`. %0A and %0D do the same. Handing the raw string to the same
 * spec-normalising parser a consumer would use closes that gap by construction. A
 * ":" rule would also be incompatible with preserving the query, since it would
 * discard any query value containing a colon.
 *
 * Anything rejected — or unparseable — falls back to /dashboard.
 */
function resolveDestination(): string {
  // The server render has no query string to read; because the result is never
  // rendered into markup, the client simply re-resolves it on hydration.
  if (typeof window === "undefined") return DEFAULT_DESTINATION;
  const raw = new URLSearchParams(window.location.search).get("next");
  if (!raw) return DEFAULT_DESTINATION;
  try {
    const url = new URL(raw, window.location.origin);
    // Off-origin: absolute, protocol-relative, or a non-http scheme (whose origin
    // parses as opaque and so can never match). This is the open-redirect case.
    if (url.origin !== window.location.origin) return DEFAULT_DESTINATION;
    // The public routes would bounce straight back here. Mirrors the list at
    // route-guard.tsx:25 — keep the two in step if a third public route lands.
    if (url.pathname === "/login" || url.pathname === "/register") {
      return DEFAULT_DESTINATION;
    }
    return `${url.pathname}${url.search}`;
  } catch {
    return DEFAULT_DESTINATION;
  }
}

export default function LoginPage() {
  const router = useRouter();
  const { status, login, loginDemo } = useAuth();
  // Public, pre-auth config: reads backend's demo and registration policies.
  const authConfig = useQuery({
    queryKey: ["auth", "config"],
    queryFn: getAuthConfig,
    staleTime: Infinity,
  }).data;
  const demoEnabled = authConfig?.demo_login_enabled;
  const registrationEnabled = authConfig?.registration_enabled ?? true;
  const isDemoOnly = Boolean(demoEnabled && !registrationEnabled);

  const [showPasswordForm, setShowPasswordForm] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // Read+clear the idle-logout flag once on mount so the notice shows exactly
  // once after an inactivity sign-out (a plain redirect would look like a bug).
  const [idleSignedOut, setIdleSignedOut] = useState(false);
  // Resolved once in a lazy initialiser, deliberately not in an effect: the
  // already-authenticated effect below runs on the *first* commit, so a value set
  // by a second effect wouldn't be there yet and an authenticated visit to
  // /login?next=/expenses would still land on /dashboard. Both redirect sites —
  // this effect and `run()` — read this one resolved value.
  const [destination] = useState(resolveDestination);

  useEffect(() => {
    if (status === "authenticated") router.replace(destination);
  }, [status, router, destination]);

  useEffect(() => {
    try {
      if (window.sessionStorage.getItem(IDLE_LOGOUT_FLAG) !== null) {
        window.sessionStorage.removeItem(IDLE_LOGOUT_FLAG);
        setIdleSignedOut(true);
      }
    } catch {
      // sessionStorage unavailable — the notice is best-effort.
    }
  }, []);

  async function run(action: () => Promise<void>) {
    setError(null);
    setSubmitting(true);
    try {
      await action();
      router.replace(destination);
    } catch (e) {
      setError(
        e instanceof ApiError ? e.detail : "Something went wrong — try again.",
      );
      setSubmitting(false); // on success we navigate away; leave the button busy
    }
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    run(() => login(email, password));
  }

  return (
    <AuthShell
      title={isDemoOnly ? "Explore the Demo" : "Sign in"}
      subtitle={
        isDemoOnly
          ? "Experience fin·tracker with preloaded accounts, spending, and investments."
          : "Welcome back to your finance tracker."
      }
      footer={
        registrationEnabled ? (
          <>
            No account?{" "}
            <Link href="/register" className="text-foreground hover:underline">
              Create one
            </Link>
          </>
        ) : null
      }
    >
      {idleSignedOut ? (
        <p className="mb-3 rounded-md border border-border bg-muted/40 px-3 py-2 text-[12px] text-muted-foreground">
          You were signed out due to inactivity. Sign in to continue.
        </p>
      ) : null}

      {isDemoOnly ? (
        <div className="flex flex-col gap-4">
          <Button
            type="button"
            size="lg"
            className="w-full"
            disabled={submitting}
            onClick={() => run(loginDemo)}
          >
            {submitting ? "Opening demo…" : "Try the demo"}
          </Button>

          {error ? (
            <p id={ERROR_ID} role="alert" className="text-[12px] text-neg">
              {error}
            </p>
          ) : null}

          <div className="pt-2">
            {!showPasswordForm ? (
              <button
                type="button"
                className="w-full text-center text-[12px] text-muted-foreground hover:text-foreground"
                onClick={() => setShowPasswordForm(true)}
              >
                Sign in with credentials
              </button>
            ) : (
              <form onSubmit={handleSubmit} className="flex flex-col gap-3 pt-2 border-t border-border">
                <Field label="Email">
                  <TextInput
                    type="email"
                    value={email}
                    onChange={setEmail}
                    autoComplete="email"
                    placeholder="you@example.com"
                    aria-describedby={error ? ERROR_ID : undefined}
                    required
                  />
                </Field>
                <Field label="Password">
                  <TextInput
                    type="password"
                    value={password}
                    onChange={setPassword}
                    autoComplete="current-password"
                    placeholder="••••••••"
                    aria-describedby={error ? ERROR_ID : undefined}
                    required
                  />
                </Field>
                <Button
                  type="submit"
                  size="sm"
                  variant="secondary"
                  className="mt-1 w-full"
                  disabled={submitting || email === "" || password === ""}
                >
                  {submitting ? "Signing in…" : "Sign in"}
                </Button>
              </form>
            )}
          </div>
        </div>
      ) : (
        <>
          <form onSubmit={handleSubmit} className="flex flex-col gap-3">
            <Field label="Email">
              <TextInput
                type="email"
                value={email}
                onChange={setEmail}
                autoComplete="email"
                placeholder="you@example.com"
                aria-describedby={error ? ERROR_ID : undefined}
                required
              />
            </Field>
            <Field label="Password">
              <TextInput
                type="password"
                value={password}
                onChange={setPassword}
                autoComplete="current-password"
                placeholder="••••••••"
                aria-describedby={error ? ERROR_ID : undefined}
                required
              />
            </Field>

            {error ? (
              <p id={ERROR_ID} role="alert" className="text-[12px] text-neg">
                {error}
              </p>
            ) : null}

            <Button
              type="submit"
              size="lg"
              className="mt-1 w-full"
              disabled={submitting || email === "" || password === ""}
            >
              {submitting ? "Signing in…" : "Sign in"}
            </Button>
          </form>

          {demoEnabled ? (
            <>
              <div className="my-4 flex items-center gap-3 text-[11px] uppercase tracking-wider text-muted-foreground">
                <span className="h-px flex-1 bg-border" />
                or
                <span className="h-px flex-1 bg-border" />
              </div>

              <Button
                type="button"
                variant="outline"
                size="lg"
                className="w-full"
                disabled={submitting}
                onClick={() => run(loginDemo)}
              >
                Try the demo
              </Button>
            </>
          ) : null}
        </>
      )}
    </AuthShell>
  );
}
