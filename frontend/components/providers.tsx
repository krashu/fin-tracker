"use client";

import { useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AuthProvider } from "@/components/auth/auth-provider";
import { IdleLogoutWatcher } from "@/components/auth/use-idle-logout";
import { BalanceVisibilityProvider } from "@/components/balance-visibility";

/**
 * Client-side data layer. Mounted by the (server) root layout so the SSR
 * theme cookie + `dark` class on <html> stay server-rendered — the client
 * boundary starts here, at the provider, not at the layout.
 *
 * The QueryClient lives in useState so it's created once per browser session
 * (a module-level singleton would be shared across requests on the server).
 */
export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // Single-user local app: data only changes via this client's own
            // mutations, so refetch-on-focus is noise. Mutations invalidate
            // explicitly (PRD §F9 propagation).
            refetchOnWindowFocus: false,
            staleTime: 30_000,
          },
        },
      }),
  );

  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <IdleLogoutWatcher />
        <BalanceVisibilityProvider>{children}</BalanceVisibilityProvider>
      </AuthProvider>
    </QueryClientProvider>
  );
}
