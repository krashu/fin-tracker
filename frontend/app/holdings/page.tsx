/**
 * /holdings — current investment positions (PRD §F7). The computed FIFO holdings
 * table and broker filter bar live in the `HoldingsView` client island; global
 * chrome comes from `AppShell` in app/layout.tsx. Read-only — no Add controls.
 */
import { Suspense } from "react";
import { PageHeader } from "@/components/shell/page-header";
import { RefreshPricesButton } from "@/components/refresh-prices-button";
import { HoldingsView } from "./holdings-view";

export default function HoldingsPage() {
  return (
    <div className="mx-auto" style={{ maxWidth: 1240, padding: "0 24px 40px" }}>
      <PageHeader
        title="Holdings"
        description="Current positions, computed FIFO from your investment transactions."
        actions={<RefreshPricesButton scope="navs" />}
      />
      <Suspense fallback={<div className="mt-6 text-[13px] text-muted-foreground">Loading holdings…</div>}>
        <HoldingsView />
      </Suspense>
    </div>
  );
}
