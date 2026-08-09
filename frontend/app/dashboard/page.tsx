/**
 * /dashboard — the Financial Overview home (PRD §F8 view 1 + view 4) and the
 * app's landing route. The data-driven bands live in the `Overview` client
 * island; global chrome (TopBar + persistent Sidebar) comes from `AppShell` in
 * app/layout.tsx. The net-worth hero is the page's headline, so there's no
 * separate PageHeader here.
 */
import { Overview } from "./overview";

export default function DashboardPage() {
  return (
    <div className="mx-auto" style={{ maxWidth: 1240, padding: "36px 24px 40px" }}>
      <Overview />
    </div>
  );
}
