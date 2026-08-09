/**
 * /settings/labels — transaction tag management (PRD §F3a). "Tags" in the UI;
 * `label` in code (the URL/dir is `labels`) to avoid colliding with F3's
 * merchant→category "tag" domain. The list + CRUD dialogs live in the
 * `LabelsManager` client island; global chrome comes from `AppShell`.
 */
import { PageHeader } from "@/components/shell/page-header";
import { LabelsManager } from "./labels-manager";

export default function LabelsPage() {
  return (
    <div className="mx-auto max-w-[1240px] px-4 pb-10 sm:px-6 lg:px-10">
      <PageHeader
        title="Tags"
        description="Freeform tags for your transactions — like #online, #restaurant, or #travel. Add them to a transaction from its row; rename or delete them here."
      />
      <LabelsManager />
    </div>
  );
}
