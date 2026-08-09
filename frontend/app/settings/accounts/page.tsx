/**
 * /settings/accounts — account management (PRD §F6). The list + CRUD dialogs
 * live in the `AccountsManager` client island; global chrome comes from
 * `AppShell` in app/layout.tsx.
 */
import { PageHeader } from "@/components/shell/page-header";
import { AccountsManager } from "./accounts-manager";

export default function AccountsPage() {
  return (
    <div className="mx-auto max-w-[1240px] px-4 pb-10 sm:px-6 lg:px-10">
      <PageHeader
        title="Accounts"
        description="Credit cards, bank accounts, cash, and investment accounts. Transactions and statement imports post to these."
      />
      <AccountsManager />
    </div>
  );
}
