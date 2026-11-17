"use client";

/**
 * Body of /imports/statements. A guest cannot upload a file (ADR-0003 rule 15), so they get
 * the bundled sample statements instead; everyone else gets the upload form. Renders nothing
 * until the session is known, so neither flashes for the wrong user.
 */
import { useAuth } from "@/components/auth/auth-provider";
import { SampleStatementPicker } from "./sample-statement-picker";
import { UploadForm } from "./upload-form";

export function StatementImport() {
  const { status, user } = useAuth();
  if (status !== "authenticated") return null;
  return user?.is_guest ? <SampleStatementPicker /> : <UploadForm />;
}
