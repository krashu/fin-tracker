"use client";

/**
 * Client-side route protection (PRD §Users & access v2).
 *
 * Auth cookies are httpOnly and set on the backend origin, so Next edge
 * middleware can't read them or run the 401→refresh flow — it would false-redirect
 * valid sessions. The gate therefore lives here, driven by the auth context.
 *
 * Public routes render bare (no app chrome) so `/login` and `/register` sit
 * outside the sidebar/top-bar shell. Everything else requires an authenticated
 * session; a transport error (`unknown`) shows a retry rather than bouncing a
 * logged-in user to `/login`.
 */
import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { AppShell } from "@/components/shell/app-shell";
import { Button } from "@/components/ui/button";
import { useAuth } from "./auth-provider";

// Two hardcoded entries, not Next route groups: with exactly two public routes,
// a plain list is simpler than restructuring the whole app/ tree. Grow into route
// groups only if a third public route (e.g. password reset) ever lands.
const PUBLIC_ROUTES = ["/login", "/register"];

function FullscreenSpinner() {
  return (
    <div className="grid min-h-screen place-items-center bg-background">
      <div
        className="size-6 animate-spin rounded-full border-2 border-border border-t-foreground"
        role="status"
        aria-label="Loading"
      />
    </div>
  );
}

function ConnectionError({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="grid min-h-screen place-items-center bg-background px-6">
      <div className="flex max-w-sm flex-col items-center gap-3 text-center">
        <p className="text-[13px] text-muted-foreground">
          Couldn’t reach the server. Check that the backend is running, then try
          again.
        </p>
        <Button type="button" variant="outline" onClick={onRetry}>
          Retry
        </Button>
      </div>
    </div>
  );
}

export function RouteGuard({
  isDark,
  children,
}: {
  isDark: boolean;
  children: ReactNode;
}) {
  const pathname = usePathname();
  const router = useRouter();
  const { status, refetch } = useAuth();
  const isPublic = PUBLIC_ROUTES.includes(pathname);

  useEffect(() => {
    if (!isPublic && status === "unauthenticated") {
      // Carry the attempted path *and* its query. This app's deep links are
      // query-driven — the ⌘K palette targets `/expenses?account=`/`?category=`
      // and the review queue reads `?present` — so a path-only `next` would drop
      // exactly the links worth preserving. Read the query off `window` rather
      // than useSearchParams(): this guard wraps the whole app from the root
      // layout, and that hook would subscribe every route to query-string changes
      // purely to serve a value this effect reads once, at redirect time. Effects
      // run only on the client, where `window.location` is always available.
      const next = `${pathname}${window.location.search}`;
      router.replace(`/login?next=${encodeURIComponent(next)}`);
    }
  }, [isPublic, pathname, status, router]);

  // Public pages own their layout — render them regardless of session state.
  if (isPublic) return <>{children}</>;

  if (status === "authenticating") return <FullscreenSpinner />;
  if (status === "unknown") return <ConnectionError onRetry={refetch} />;
  if (status === "unauthenticated") return null; // redirect handled in the effect

  return <AppShell isDark={isDark}>{children}</AppShell>;
}
