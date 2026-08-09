/**
 * /investments — investment transaction log + manual entry (PRD §F7). The
 * data-driven half lives in the `InvestmentsBoard` client island; global chrome
 * comes from `AppShell` in app/layout.tsx. The Add controls sit in the page
 * header's actions slot.
 */
import { PageHeader } from "@/components/shell/page-header";
import { InvestmentsBoard } from "./investments-board";
import { InvestmentAddControls } from "./add-transaction";

export default function InvestmentsPage() {
  return (
    <div className="mx-auto" style={{ maxWidth: 1240, padding: "0 24px 40px" }}>
      <PageHeader
        title="Investments"
        description="Your investment transaction log — buys, SIPs, sells, dividends and bonuses."
        actions={<InvestmentAddControls />}
      />
      <InvestmentsBoard />
    </div>
  );
}
