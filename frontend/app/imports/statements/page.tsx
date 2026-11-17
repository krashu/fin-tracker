/**
 * /imports/statements — the statement-upload entry point (the /expenses "Import
 * statement" button links here). The body is the `StatementImport` client island: the
 * upload form, or the bundled samples for a guest. Global chrome comes from `AppShell`
 * in app/layout.tsx.
 */
import { PageHeader } from "@/components/shell/page-header";
import { ImportStepper } from "@/components/ui/stepper";
import { StatementImport } from "./statement-import";

export default function StatementsPage() {
  return (
    <div className="mx-auto" style={{ maxWidth: 1240, padding: "0 40px 40px" }}>
      <PageHeader
        title="Import statement"
        description="Import a credit-card or bank statement to extract transactions for review."
      />
      <div className="pb-6">
        <ImportStepper current="upload" />
      </div>
      <StatementImport />
    </div>
  );
}
