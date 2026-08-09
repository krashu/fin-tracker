/**
 * /spending — the spending analysis dashboard (PRD §F8 views 2 + 3). The
 * data-driven charts live in the `SpendingDashboard` client island; global
 * chrome comes from `AppShell` in app/layout.tsx.
 */
import { PageHeader } from "@/components/shell/page-header";
import { SpendingDashboard } from "./spending-dashboard";

export default function SpendingPage() {
  return (
    <div className="mx-auto" style={{ maxWidth: 1240, padding: "0 24px 40px" }}>
      <PageHeader
        title="Spending"
        description="Category breakdown and spend-over-time trends."
      />
      <SpendingDashboard />
    </div>
  );
}
