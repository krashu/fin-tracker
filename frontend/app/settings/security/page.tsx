/**
 * /settings/security — change the account password (PRD §Users & access v2) or delete
 * the account (ADR-0003 rule 13b). Each card lives in its own client island
 * (`SecurityManager`, `DeleteAccountCard`); global chrome comes from `AppShell` in
 * app/layout.tsx.
 */
import { PageHeader } from "@/components/shell/page-header";
import { DeleteAccountCard } from "./delete-account-card";
import { SecurityManager } from "./security-manager";

export default function SecurityPage() {
  return (
    <div className="mx-auto max-w-[1240px] px-4 pb-10 sm:px-6 lg:px-10">
      <PageHeader
        title="Security"
        description="Change your password, or delete your account."
      />
      <div className="flex flex-col gap-5">
        <SecurityManager />
        <DeleteAccountCard />
      </div>
    </div>
  );
}
