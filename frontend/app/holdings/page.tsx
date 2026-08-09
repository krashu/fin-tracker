/**
 * /holdings — current investment positions (PRD §F7). The computed FIFO holdings
 * table lives in the `HoldingsTable` client island; global chrome comes from
 * `AppShell` in app/layout.tsx. Read-only — no Add controls.
 */
import { PageHeader } from "@/components/shell/page-header";
import { RefreshPricesButton } from "@/components/refresh-prices-button";
import { HoldingsTable } from "./holdings-table";

export default function HoldingsPage() {
  return (
    <div className="mx-auto" style={{ maxWidth: 1240, padding: "0 24px 40px" }}>
      <PageHeader
        title="Holdings"
        description="Current positions, computed FIFO from your investment transactions."
        actions={<RefreshPricesButton scope="navs" />}
      />
      <HoldingsTable />
    </div>
  );
}
