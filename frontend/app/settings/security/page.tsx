/**
 * /settings/security — change the account password (PRD §Users & access v2). The form
 * lives in the `SecurityManager` client island; global chrome comes from `AppShell` in
 * app/layout.tsx.
 */
import { PageHeader } from "@/components/shell/page-header";
import { SecurityManager } from "./security-manager";

export default function SecurityPage() {
  return (
    <div className="mx-auto max-w-[1240px] px-4 pb-10 sm:px-6 lg:px-10">
      <PageHeader
        title="Security"
        description="Change your account password. Your other devices are signed out shortly after."
      />
      <SecurityManager />
    </div>
  );
}
