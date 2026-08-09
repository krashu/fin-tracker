/**
 * /portfolio — investment portfolio dashboard (PRD §F8 views 5 & 6). KPI tiles
 * (value · invested · unrealized P&L · XIRR) + an asset-class allocation donut
 * (GET /portfolio/summary), then the scalar "am I beating the market" comparison
 * vs a chosen index fund (GET /portfolio/performance). Both are client islands;
 * global chrome (TopBar + Sidebar) comes from `AppShell` in app/layout.tsx. Read-only.
 */
import { PageHeader } from "@/components/shell/page-header";
import { RefreshPricesButton } from "@/components/refresh-prices-button";
import { PortfolioPerformance } from "./portfolio-performance";
import { PortfolioSummary } from "./portfolio-summary";

export default function PortfolioPage() {
  return (
    <div className="mx-auto" style={{ maxWidth: 1240, padding: "0 24px 40px" }}>
      <PageHeader
        title="Portfolio"
        description="Value, returns (XIRR), allocation, and how you’re tracking against the market."
        actions={<RefreshPricesButton />}
      />
      <div className="flex flex-col gap-5">
        <PortfolioSummary />
        <PortfolioPerformance />
      </div>
    </div>
  );
}
