/**
 * /imports/review — the pending-review hub: a list of open import batches (≥1
 * unconfirmed row), each linking into its own review queue at
 * /imports/review/[batchId]. This is the discoverable entry point the sidebar
 * "Review" item, the Expenses "Review N pending" button, and the ⌘K palette all
 * target — before it existed, the top-bar notification bell was the only way
 * back after navigating away from a fresh upload. The batch list itself is the
 * `PendingBatches` client island (shares the `["imports","pending"]` cache with
 * the bell); global chrome comes from `AppShell` in app/layout.tsx.
 */
import { PageHeader } from "@/components/shell/page-header";
import { PendingBatches } from "./pending-batches";

export default function ReviewIndexPage() {
  return (
    <div className="mx-auto" style={{ maxWidth: 1240, padding: "0 40px 40px" }}>
      <PageHeader
        title="Review imports"
        description="Imported statements waiting for you to tag and commit. Pick a batch to review its transactions."
      />
      <PendingBatches />
    </div>
  );
}
