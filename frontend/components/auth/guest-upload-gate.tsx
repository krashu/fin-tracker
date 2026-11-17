"use client";

/**
 * Hides an upload action from a guest (ADR-0003 rule 15). The guest sandbox owns synthetic
 * data only, so every upload route refuses a guest with a 403; this gate removes the action
 * up front rather than letting the user pick a file and then be refused.
 *
 * Renders nothing until the session is known (so the upload never flashes for a guest),
 * the notice for a guest, and `children` for everyone else.
 */
import Link from "next/link";
import type { ReactNode } from "react";

import { useAuth } from "@/components/auth/auth-provider";

export function GuestUploadGate({
  children,
  notice,
}: {
  children: ReactNode;
  /** Replaces the default notice for a guest. */
  notice?: ReactNode;
}) {
  const { status, user } = useAuth();
  if (status !== "authenticated") return null;
  if (user?.is_guest) {
    return (
      <p className="max-w-xl text-[12.5px] text-muted-foreground">
        {notice ?? (
          <>
            Guests can’t upload files.{" "}
            <Link href="/imports/statements" className="underline underline-offset-2">
              Try a sample statement
            </Link>
            .
          </>
        )}
      </p>
    );
  }
  return <>{children}</>;
}
