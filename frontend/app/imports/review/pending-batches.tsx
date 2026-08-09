"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";

import { listPendingImports } from "@/lib/api/client";
import { pendingBatchLabel } from "@/lib/accounts";
import {
  IconAlert,
  IconCheckAll,
  IconChevronRight,
} from "@/components/icons";

/** Shared card shell so the loading / error / empty / list states share one frame. */
function Card({ children }: { children: React.ReactNode }) {
  return (
    <div className="overflow-hidden rounded-[6px] border border-border bg-card">
      {children}
    </div>
  );
}

/** Centered message row for the non-list states (loading / error / empty). */
function Notice({
  icon,
  children,
}: {
  icon?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <p className="flex items-center justify-center gap-2 px-4 py-16 text-center text-[13px] text-muted-foreground">
      {icon}
      {children}
    </p>
  );
}

/**
 * The list of open import batches. Branches on query state so a failed or
 * still-loading fetch never renders the affirmative "all caught up" — that
 * state is reserved for a settled, genuinely-empty result. Reuses the
 * `["imports","pending"]` query (kept live by the upload / commit / cancel
 * invalidations), so this stays in sync with the top-bar bell for free.
 */
export function PendingBatches() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["imports", "pending"],
    queryFn: listPendingImports,
  });

  if (isLoading) {
    return (
      <Card>
        <Notice>Loading pending imports…</Notice>
      </Card>
    );
  }

  if (isError) {
    return (
      <Card>
        <Notice icon={<IconAlert className="size-4 text-muted-foreground" />}>
          Couldn’t load pending imports. Try refreshing.
        </Notice>
      </Card>
    );
  }

  const batches = data ?? [];

  if (batches.length === 0) {
    return (
      <Card>
        <Notice icon={<IconCheckAll className="size-4 text-pos" />}>
          You’re all caught up — nothing waiting for review.
        </Notice>
      </Card>
    );
  }

  return (
    <Card>
      <ul className="divide-y divide-border">
        {batches.map((b) => (
          <li key={b.batch_id}>
            <Link
              href={`/imports/review/${b.batch_id}`}
              className="flex items-center gap-3 px-4 py-3 transition-colors hover:bg-muted focus-visible:bg-muted focus-visible:outline-none"
            >
              <span
                className="min-w-0 flex-1 truncate text-[13px] font-medium text-foreground"
                style={{ letterSpacing: "-0.005em" }}
              >
                {pendingBatchLabel(b.account_name, b.account_last4)}
              </span>
              <span className="shrink-0 tabular-nums text-[12px] text-muted-foreground">
                {b.pending_count} pending
              </span>
              <IconChevronRight className="size-4 shrink-0 text-muted-foreground/60" />
            </Link>
          </li>
        ))}
      </ul>
    </Card>
  );
}
