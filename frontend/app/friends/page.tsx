import { Suspense } from "react";
import { PageHeader } from "@/components/shell/page-header";
import { FriendsDashboard } from "./friends-dashboard";

/**
 * /friends — Friends & Khata (Peer Ledger) Dashboard (PRD §F3b, ADR-0017).
 *
 * Tracks peer receivables ("You will get") and payables ("You will give"),
 * credit card fronting for friends, and personal Khatabook debt balances.
 */
export default function FriendsPage() {
  return (
    <div className="mx-auto" style={{ maxWidth: 1240, padding: "0 24px 40px" }}>
      <PageHeader
        title="Friends & Khata"
        description="Track shared expenses, credit card fronting, and peer debts."
      />
      <Suspense fallback={<div className="py-12 text-center text-[13px] text-muted-foreground">Loading ledger…</div>}>
        <FriendsDashboard />
      </Suspense>
    </div>
  );
}
