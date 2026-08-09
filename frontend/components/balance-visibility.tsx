"use client";

/**
 * Privacy toggle for over-the-shoulder / screen-share: hide every money amount
 * and balance behind `••••` (and blur chart magnitudes) without leaving the
 * page.
 *
 * Client Context + localStorage — NOT a cookie + `router.refresh()` like the
 * theme toggle. A frequently-flicked mask must be instant; a server refresh per
 * toggle (re-running every server component) would be slow and would need the
 * flag threaded through six server page shells. The amounts all live in client
 * islands, so client state reaches them directly.
 *
 * The persisted value loads in an effect (localStorage is client-only; reading
 * it in the initializer would desync from the server-rendered default and warn
 * on hydration). We default to **hidden** so a reload never flashes real amounts
 * to someone who had them masked (over-the-shoulder is exactly when a reload
 * happens). The trade is the reverse flicker — a visible user sees `••••` for one
 * frame on every load until the effect resolves — which is harmless: it lands on
 * users who, by definition, don't mind seeing their balances.
 */
import { createContext, useContext, useEffect, useState } from "react";

import { cn } from "@/lib/utils";

const STORAGE_KEY = "balance-hidden";

type BalanceVisibility = { hidden: boolean; toggle: () => void };

const BalanceVisibilityContext = createContext<BalanceVisibility>({
  hidden: true,
  toggle: () => {},
});

export function BalanceVisibilityProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  // Default hidden (see file docstring): the first paint must not leak real
  // amounts. The effect below resolves the persisted state on mount, unmasking
  // for users who haven't opted into hiding.
  const [hidden, setHidden] = useState(true);

  useEffect(() => {
    setHidden(window.localStorage.getItem(STORAGE_KEY) === "1");
  }, []);

  const toggle = () =>
    setHidden((h) => {
      const next = !h;
      window.localStorage.setItem(STORAGE_KEY, next ? "1" : "0");
      return next;
    });

  return (
    <BalanceVisibilityContext.Provider value={{ hidden, toggle }}>
      {children}
    </BalanceVisibilityContext.Provider>
  );
}

export function useBalanceHidden(): BalanceVisibility {
  return useContext(BalanceVisibilityContext);
}

/**
 * Wrap a formatted money/quantity value. Renders it as-is when visible; when
 * hidden, renders `••••` in its place (inheriting the surrounding type styles).
 * Use for textual amounts; for chart magnitudes (bars, sparkline) read
 * `useBalanceHidden()` directly and apply a blur instead.
 */
export function Sensitive({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  const { hidden } = useBalanceHidden();
  if (!hidden) return <>{children}</>;
  return (
    <span aria-label="Amount hidden" className={cn("select-none", className)}>
      ••••
    </span>
  );
}
