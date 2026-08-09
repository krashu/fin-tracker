/**
 * /settings/categories — category management (PRD §F5). The list + CRUD dialogs
 * live in the `CategoriesManager` client island; global chrome comes from
 * `AppShell` in app/layout.tsx.
 */
import { PageHeader } from "@/components/shell/page-header";
import { CategoriesManager } from "./categories-manager";

export default function CategoriesPage() {
  return (
    <div className="mx-auto max-w-[1240px] px-4 pb-10 sm:px-6 lg:px-10">
      <PageHeader
        title="Categories"
        description="Categories for tagging spending and income. Defaults are seeded — add your own, or edit any category's name and color."
      />
      <CategoriesManager />
    </div>
  );
}
