"use client";

/**
 * Auth session context (PRD §Users & access v2).
 *
 * Holds the current user, sourced from `GET /auth/me`. Tokens live in httpOnly
 * cookies (unreadable from JS), so "am I logged in?" is answered by whether that
 * call succeeds — not by inspecting any client-side token.
 *
 * The status is deliberately three-valued, NOT a boolean. A failed `/me` is not
 * always "logged out": a transport error (backend still booting, a flaky corp
 * proxy) must leave us in `unknown` so the guard shows a retry instead of
 * bouncing an authenticated user to `/login`. Only a real `401` means
 * `unauthenticated`.
 *
 * On every auth transition we `cancelQueries()` → `clear()` → reseed `me` so a
 * previous user's in-flight response can't settle back into the cache after a
 * switch (isolation). `client.ts` handles the silent access-token refresh; this
 * provider only reacts to a *failed* refresh via `setAuthFailureHandler`.
 */
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  type ReactNode,
} from "react";
import {
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";

import {
  ApiError,
  DEMO_CREDENTIALS,
  demoSession as apiDemoSession,
  login as apiLogin,
  logout as apiLogout,
  me,
  register as apiRegister,
  setAuthFailureHandler,
  type AuthUser,
} from "@/lib/api/client";

const ME_KEY = ["auth", "me"] as const;

export type AuthStatus =
  | "authenticating" // initial /me in flight, outcome unknown
  | "authenticated"
  | "unauthenticated" // a real 401 — no valid session
  | "unknown"; // transport error — do NOT treat as logged out

type AuthContextValue = {
  user: AuthUser | null;
  status: AuthStatus;
  login: (email: string, password: string) => Promise<void>;
  loginDemo: () => Promise<void>;
  register: (
    email: string,
    password: string,
    displayName?: string,
  ) => Promise<void>;
  logout: () => Promise<void>;
  /** Force a re-check of `/me` (the guard's retry affordance for `unknown`). */
  refetch: () => void;
};

const AuthContext = createContext<AuthContextValue | null>(null);

/** Reset the whole query cache and reseed `me` on an auth transition. The
 * `cancelQueries` marks in-flight queries cancelled so TanStack *discards* their
 * late results (the requests aren't aborted on the wire — no AbortSignal is
 * threaded), which is what keeps a previous user's response from repopulating the
 * cache after the switch. */
async function resetSession(qc: QueryClient, user: AuthUser | null) {
  // Seed the new session identity FIRST, in place, on the always-mounted `me`
  // observer (in <AuthProvider>). This must not go through a remove-then-recreate:
  // `qc.clear()` / an unfiltered `removeQueries()` destroys the very `me` query the
  // observer is subscribed to, and the observer does NOT reconnect to the fresh
  // query `setQueryData` creates in time — it keeps reporting stale `undefined`, so
  // a just-authenticated user reads as `unauthenticated` and the route guard bounces
  // them straight back to /login (the "Try the demo does nothing" bug).
  qc.setQueryData(ME_KEY, user);
  // Isolation: cancel the previous user's in-flight queries (so a late response can't
  // settle back into the cache post-switch) and drop their cached data — everything
  // EXCEPT the `me` entry we just seeded.
  await qc.cancelQueries();
  qc.removeQueries({
    predicate: (q) => !(q.queryKey[0] === "auth" && q.queryKey[1] === "me"),
  });
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();

  const meQuery = useQuery({
    queryKey: ME_KEY,
    queryFn: me,
    staleTime: Infinity,
    // Retry only transport failures, never a 401 (which is a definitive answer).
    retry: (failureCount, error) =>
      !(error instanceof ApiError) && failureCount < 2,
  });

  const status: AuthStatus = useMemo(() => {
    if (meQuery.data) return "authenticated";
    if (meQuery.data === null) return "unauthenticated"; // seeded on logout/failure
    if (meQuery.isPending) return "authenticating";
    if (meQuery.error instanceof ApiError) {
      return meQuery.error.status === 401 ? "unauthenticated" : "unknown";
    }
    if (meQuery.error) return "unknown"; // network / transport
    return "authenticating";
  }, [meQuery.data, meQuery.isPending, meQuery.error]);

  // Mid-session: a query 401'd and the silent refresh failed. Drop the session
  // (→ guard redirects). Idempotent — only fires when we currently hold a user,
  // so a fan-out of concurrent 401s collapses to one transition.
  useEffect(() => {
    return setAuthFailureHandler(() => {
      if (qc.getQueryData<AuthUser>(ME_KEY) != null) {
        qc.setQueryData(ME_KEY, null);
      }
    });
  }, [qc]);

  const value = useMemo<AuthContextValue>(
    () => ({
      user: meQuery.data ?? null,
      status,
      login: async (email, password) => {
        const user = await apiLogin(email, password);
        await resetSession(qc, user);
      },
      loginDemo: async () => {
        let user: AuthUser;
        try {
          user = await apiDemoSession();
        } catch {
          user = await apiLogin(
            DEMO_CREDENTIALS.email,
            DEMO_CREDENTIALS.password,
          );
        }
        await resetSession(qc, user);
      },
      register: async (email, password, displayName) => {
        const user = await apiRegister(email, password, displayName);
        await resetSession(qc, user);
      },
      logout: async () => {
        try {
          await apiLogout();
        } finally {
          await resetSession(qc, null);
        }
      },
      refetch: () => void meQuery.refetch(),
    }),
    [qc, status, meQuery.data, meQuery.refetch],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (ctx === null) {
    throw new Error("useAuth must be used within <AuthProvider>");
  }
  return ctx;
}
