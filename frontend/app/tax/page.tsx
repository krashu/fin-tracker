/**
 * /tax - the F11 statement (PRD §F11). Today: the intraday listing only; capital gains
 * and dividends land in v0.8. The data-driven part lives in the `TaxStatement` client
 * island; global chrome comes from `AppShell` in app/layout.tsx.
 */
import { PageHeader } from "@/components/shell/page-header";
import { TaxStatement } from "./tax-statement";

export default function TaxPage() {
  return (
    <div className="mx-auto" style={{ maxWidth: 1240, padding: "0 24px 40px" }}>
      <PageHeader
        title="Tax statement"
        description="A statement to reconcile against your broker's report and AIS before filing. Not tax advice."
      />
      <TaxStatement />
    </div>
  );
}
