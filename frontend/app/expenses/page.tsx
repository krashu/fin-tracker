/**
 * /expenses — the spend + refund board (PRD §F8 / F2). Design baseline chosen
 * 2026-05-25 after a 4-variant exploration (see git history of
 * `frontend/app/looks/*`): content-first, Hanken Grotesk + JetBrains Mono for
 * tabular amounts, indigo --primary (locked 2026-05-24).
 *
 * Renamed /transactions → /expenses (2026-05-25), narrowed to spend+refund rows;
 * income/transfer live elsewhere. Pending-tag rows live at
 * /imports/review/[batchId] only — this board shows confirmed txns.
 *
 * The data-driven half lives in the `ExpensesBoard` client island; this file is
 * the page shell. Global chrome (TopBar + persistent Sidebar) is provided once
 * by `AppShell` in app/layout.tsx — the Add / Import controls sit in the page
 * header's actions slot.
 */
import { Suspense } from "react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/shell/page-header";
import { IconUpload } from "@/components/icons";
import { ExpensesBoard } from "./expenses-board";
import { AddControls } from "./add-transaction";
import { ReviewPendingButton } from "./review-pending-button";

export default function ExpensesPage() {
  return (
    <div className="mx-auto" style={{ maxWidth: 1240, padding: "0 24px 40px" }}>
      <PageHeader
        title="Expenses"
        actions={
          <>
            <ReviewPendingButton />
            <AddControls />
            <Button
              asChild
              className="h-7 gap-1.5 px-2.5 text-[12px] font-medium"
            >
              <Link href="/imports/statements">
                <IconUpload className="size-3" />
                Import statement
              </Link>
            </Button>
          </>
        }
      />
      {/* ExpensesBoard reads useSearchParams (?account/?category deep-links from
          the ⌘K palette); a Suspense boundary is required so the route still
          prerenders (Next bails the subtree to client-render inside it).
          SummaryStrip is rendered inside ExpensesBoard so it can follow the
          board's year/month/allDates filter state. */}
      <Suspense>
        <ExpensesBoard />
      </Suspense>
    </div>
  );
}
