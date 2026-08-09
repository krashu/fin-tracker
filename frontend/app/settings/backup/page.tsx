/**
 * /settings/backup — download a CSV backup of spend transactions (with their accounts and
 * categories) and load one back (PRD §F10). The download/upload logic lives in the
 * `BackupManager` client island; global chrome comes from `AppShell` in app/layout.tsx.
 */
import { PageHeader } from "@/components/shell/page-header";
import { BackupManager } from "./backup-manager";

export default function BackupPage() {
  return (
    <div className="mx-auto max-w-[1240px] px-4 pb-10 sm:px-6 lg:px-10">
      <PageHeader
        title="Backup"
        description="Download a backup of your spend transactions — with the accounts and categories they use — or load one back. Loading is additive: transactions already present are skipped, so nothing is overwritten."
      />
      <BackupManager />
    </div>
  );
}
