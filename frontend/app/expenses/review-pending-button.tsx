"use client";

/**
 * Expenses header "Review N pending" button — a contextual entry point into the
 * import review hub (/imports/review). Renders nothing when nothing is pending,
 * so the header only carries it when there's work to do. Shares the
 * `["imports","pending"]` cache with the top-bar bell and the sidebar badge, so
 * it stays live (upload / commit / cancel invalidate the key) and adds no
 * request. On mobile the sidebar rail is hidden, so this is the primary
 * discoverable entry point on the Expenses page.
 */
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { IconChevronRight } from "@/components/icons";
import { listPendingImports } from "@/lib/api/client";

export function ReviewPendingButton() {
  const { data } = useQuery({
    queryKey: ["imports", "pending"],
    queryFn: listPendingImports,
  });
  const total = (data ?? []).reduce((n, b) => n + b.pending_count, 0);

  if (total === 0) return null;

  return (
    <Button
      asChild
      variant="outline"
      className="h-7 gap-1 px-2.5 text-[12px] font-medium"
    >
      <Link href="/imports/review">
        Review {total} pending
        <IconChevronRight className="size-3" />
      </Link>
    </Button>
  );
}
