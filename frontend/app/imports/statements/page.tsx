/**
 * /imports/statements — the statement-upload entry point (the /expenses "Import
 * statement" button links here). The form is the `UploadForm` client island;
 * global chrome comes from `AppShell` in app/layout.tsx.
 */
import { PageHeader } from "@/components/shell/page-header";
import { ImportStepper } from "@/components/ui/stepper";
import { UploadForm } from "./upload-form";

export default function StatementsPage() {
  return (
    <div className="mx-auto" style={{ maxWidth: 1240, padding: "0 40px 40px" }}>
      <PageHeader
        title="Import statement"
        description="Upload a credit-card or bank statement PDF to extract transactions for review."
      />
      <div className="pb-6">
        <ImportStepper current="upload" />
      </div>
      <UploadForm />
    </div>
  );
}
