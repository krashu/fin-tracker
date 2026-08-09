/**
 * /imports/investments — the investment-transaction CSV upload entry point (PRD §F7).
 * Like the CAS flow before it (and unlike the statement flow) there's no review-queue
 * stepper: investments have no categories to tag, so it commits directly and the form
 * shows the import summary inline. The form is the `CsvUploadForm` client island;
 * global chrome (TopBar + persistent Sidebar) comes from `AppShell` in app/layout.tsx.
 */
import { PageHeader } from "@/components/shell/page-header";
import { CsvUploadForm } from "./csv-upload-form";

export default function InvestmentCsvImportPage() {
  return (
    <div className="mx-auto" style={{ maxWidth: 1240, padding: "0 40px 40px" }}>
      <PageHeader
        title="Import transactions"
        description="Upload a broker transaction export (e.g. a Zerodha Console tradebook) as CSV. Columns are matched by name — date, type, symbol, units, price (amount/fees/name/exchange optional) — so most raw exports import without renaming."
      />
      <CsvUploadForm />
    </div>
  );
}
