/**
 * /settings/rules — learned auto-tag rules (PRD §F3 / §F3a). Shows the two
 * per-merchant memory tables fin-tracker builds as you confirm imports:
 * merchant→category (one winner prefilled) and merchant→tag (each auto-applies
 * once it's seen enough). Lets you prune a bad association. NOT F4a
 * reconciliation rules and NOT user-authored regex rules (out of v1). The list +
 * delete dialogs live in the `RulesManager` client island.
 */
import { PageHeader } from "@/components/shell/page-header";
import { RulesManager } from "./rules-manager";

export default function RulesPage() {
  return (
    <div className="mx-auto max-w-[1240px] px-4 pb-10 sm:px-6 lg:px-10">
      <PageHeader
        title="Rules"
        description="What fin-tracker has learned from your imports — which category and tags to suggest for each merchant. Delete a rule to make it forget; it re-learns the next time you confirm that merchant."
      />
      <RulesManager />
    </div>
  );
}
