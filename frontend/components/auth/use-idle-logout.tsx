"use client";

/**
 * Idle-logout watcher (PRD §Users & access v2).
 *
 * Signs the user out after {@link IDLE_TIMEOUT_MS} of no real interaction, both
 * ending the session server-side and clearing the exposed financial screen.
 *
 * Why a client timer and not a shorter backend refresh TTL: the refresh window
 * slides on every silent refresh, and the dashboard's 30s poll (overview.tsx)
 * keeps refreshing a left-open tab indefinitely — so the backend never sees the
 * tab as idle. Genuine "user is idle" can only be observed from input events here.
 *
 * The check is interval-driven off a stored timestamp rather than a per-event
 * `setTimeout` reset: it stays cheap under a mousemove firehose and, unlike a
 * suspended timeout, fires correctly after the laptop sleeps past the window.
 *
 * Last-activity lives in `localStorage` so it's shared across tabs — activity in
 * any tab keeps them all alive, so an idle second tab can't revoke a session the
 * user is actively using in the first.
 */
import { useEffect, useRef } from "react";

import { useAuth, type AuthStatus } from "./auth-provider";

export const IDLE_TIMEOUT_MS = 60 * 60 * 1000; // 60 min
const CHECK_INTERVAL_MS = 15_000;
const WRITE_THROTTLE_MS = 5_000;

const ACTIVITY_KEY = "fin-tracker:last-activity";
/** Set just before an idle logout; the login page reads+clears it to show the
 * "signed out due to inactivity" notice instead of a bare, bug-looking redirect. */
export const IDLE_LOGOUT_FLAG = "fin-tracker:idle-logout";

// Passive, high-frequency signals of a present user. `scroll`/`wheel` cover
// reading without moving the pointer; `touchstart` covers mobile.
const ACTIVITY_EVENTS = [
  "mousedown",
  "mousemove",
  "keydown",
  "touchstart",
  "scroll",
  "wheel",
] as const;

function readLastActivity(): number {
  try {
    const raw = window.localStorage.getItem(ACTIVITY_KEY);
    const parsed = raw === null ? NaN : Number(raw);
    return Number.isFinite(parsed) ? parsed : 0;
  } catch {
    return 0; // storage disabled/full — treat as "no record", the interval reseeds
  }
}

function writeLastActivity(ts: number): void {
  try {
    window.localStorage.setItem(ACTIVITY_KEY, String(ts));
  } catch {
    // Storage unavailable: the in-memory ref still gates single-tab logout.
  }
}

/**
 * Arm the idle watcher while authenticated. Calls `logout` after the timeout of
 * no interaction. A no-op in every other auth state (so `/login` itself is never
 * watched, and a transport-`unknown` state doesn't log anyone out).
 */
export function useIdleLogout(
  status: AuthStatus,
  logout: () => Promise<void>,
): void {
  // Hold `logout` in a ref so the arming effect depends only on `status`, not on
  // `logout`'s identity. `logout` is stable while authenticated *today* (the `me`
  // query is `staleTime: Infinity`), but if it ever churned — e.g. a future
  // focus-refetch of `me` — an identity change would re-run the effect and
  // re-seed the activity clock every time, silently disabling the logout. Arm
  // once per real auth transition instead.
  const logoutRef = useRef(logout);
  logoutRef.current = logout;

  useEffect(() => {
    if (status !== "authenticated") return;

    // Seed to now so a stale timestamp from a previous session can't trip an
    // immediate logout the moment this session arms.
    let localLast = Date.now();
    let lastWrite = localLast;
    writeLastActivity(localLast);

    let fired = false;

    const markActive = () => {
      const now = Date.now();
      localLast = now;
      // Throttle the cross-tab write — a 60-min window doesn't need per-mousemove
      // persistence, and localStorage writes are synchronous.
      if (now - lastWrite >= WRITE_THROTTLE_MS) {
        lastWrite = now;
        writeLastActivity(now);
      }
    };

    for (const ev of ACTIVITY_EVENTS) {
      window.addEventListener(ev, markActive, { passive: true });
    }

    const interval = window.setInterval(() => {
      if (fired) return;
      // Trust whichever record is newer: this tab's ref or another tab's write.
      const lastActivity = Math.max(localLast, readLastActivity());
      if (Date.now() - lastActivity >= IDLE_TIMEOUT_MS) {
        fired = true; // guard the async gap so the next tick can't double-fire
        try {
          window.sessionStorage.setItem(IDLE_LOGOUT_FLAG, "1");
        } catch {
          // Notice is best-effort; the logout itself still proceeds.
        }
        // Fire-and-forget: logout() resets the session in its own `finally` even
        // if the network revoke throws, so just swallow the rejection here.
        void logoutRef.current().catch(() => {});
      }
    }, CHECK_INTERVAL_MS);

    return () => {
      window.clearInterval(interval);
      for (const ev of ACTIVITY_EVENTS) {
        window.removeEventListener(ev, markActive);
      }
    };
  }, [status]);
}

/** Mounts the idle watcher inside <AuthProvider>. Renders nothing. */
export function IdleLogoutWatcher(): null {
  const { status, logout } = useAuth();
  useIdleLogout(status, logout);
  return null;
}
