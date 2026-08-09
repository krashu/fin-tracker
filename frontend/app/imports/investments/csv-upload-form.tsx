"use client";

/**
 * Investment CSV upload (PRD §F7). Choose a broker transaction export (e.g. a Zerodha
 * Console tradebook), pick the asset class for the file → POST /imports/investments.
 * Account-less with no review queue, so on success we stay and render a summary inline:
 * counts plus any PII-safe per-row warnings (line number + reason). A repo-tracked
 * header-alias map means the raw export usually needs no column renaming.
 */
import { useState } from "react";
import Link from "next/link";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { IconDoc, IconUpload } from "@/components/icons";
import { Field } from "@/components/form/fields";
import {
  ApiError,
  createInvestmentCsvImport,
  type AssetClass,
  type InvestmentCsvImportSummary,
} from "@/lib/api/client";
import { ASSET_CLASS_LABELS } from "@/lib/investments";
import { cn } from "@/lib/utils";

// Derived from the one label map (as add-instrument.tsx already does) rather than a
// second hand-written list. The old array had drifted on five of nine labels — the same
// class read "Indian stocks" here and "Indian equity" on /holdings — and its
// `{value,label}[]` type is satisfied by any subset, so a 10th asset class would have
// silently had no option here while tsc caught it in the Record.
const ASSET_CLASSES = Object.keys(ASSET_CLASS_LABELS) as AssetClass[];

export function CsvUploadForm() {
  const queryClient = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [assetClass, setAssetClass] = useState<AssetClass>("indian_equity");
  const [summary, setSummary] = useState<InvestmentCsvImportSummary | null>(
    null,
  );

  const mutation = useMutation({
    mutationFn: (vars: { file: File; asset_class: AssetClass }) =>
      createInvestmentCsvImport(vars),
    onSuccess: (s) => {
      setSummary(s);
      // A fresh import writes instruments + investment txns, so the cached
      // Transactions / Holdings / Portfolio / dashboard net-worth views are now
      // stale (PRD §F9). Mirror the manual-entry nudge; skip when the file was a
      // duplicate (nothing changed). Prefix keys fan out to all descendants.
      if (!s.already_imported) {
        queryClient.invalidateQueries({
          queryKey: ["investment-transactions"],
        });
        queryClient.invalidateQueries({ queryKey: ["instruments"] });
        queryClient.invalidateQueries({ queryKey: ["holdings"] });
        queryClient.invalidateQueries({ queryKey: ["portfolio"] });
        queryClient.invalidateQueries({ queryKey: ["dashboards"] });
      }
    },
  });

  function handleUpload() {
    if (file == null) return;
    setSummary(null);
    mutation.mutate({ file, asset_class: assetClass });
  }

  const canSubmit = file != null && !mutation.isPending;

  return (
    <div className="flex max-w-xl flex-col gap-4">
      <Field label="Transaction CSV">
        {/* sr-only (not hidden) keeps the file input in the tab order + a11y tree;
            the visible label is the button and shows the focus ring via has-[]. */}
        <label className="flex cursor-pointer items-center gap-2 rounded-md border border-border bg-background px-2.5 py-2 text-[12.5px] transition-colors hover:bg-muted/50 has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-ring">
          <input
            type="file"
            accept=".csv,text/csv"
            className="sr-only"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
          <IconDoc className="size-3.5 shrink-0 text-muted-foreground" />
          <span
            className={cn(
              "truncate",
              file ? "text-foreground" : "text-muted-foreground",
            )}
          >
            {file ? file.name : "Choose a CSV…"}
          </span>
        </label>
      </Field>

      <Field label="Asset class for this file">
        <select
          value={assetClass}
          onChange={(e) => setAssetClass(e.target.value as AssetClass)}
          className="w-full rounded-md border border-border bg-background px-2.5 py-2 text-[12.5px] text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {ASSET_CLASSES.map((c) => (
            <option key={c} value={c}>
              {ASSET_CLASS_LABELS[c]}
            </option>
          ))}
        </select>
      </Field>
      <p className="-mt-2 text-[11.5px] text-muted-foreground">
        Applied to rows without an asset-class column. Prices in INR or USD
        import; a row in any other currency is skipped with a warning (the rest
        of the file still imports). US stocks/ETFs must be priced in USD.
      </p>

      {mutation.isError ? (
        <p className="text-[12px] text-neg">
          {mutation.error instanceof ApiError
            ? mutation.error.detail
            : "Couldn’t import — try again."}
        </p>
      ) : null}

      {summary ? <CsvSummary summary={summary} /> : null}

      <div>
        <Button
          type="button"
          onClick={handleUpload}
          disabled={!canSubmit}
          className="h-9 gap-1.5 px-3.5 text-[12.5px] font-medium"
        >
          <IconUpload className="size-3.5" />
          {mutation.isPending ? "Importing…" : "Import transactions"}
        </Button>
      </div>
    </div>
  );
}

function CsvSummary({ summary }: { summary: InvestmentCsvImportSummary }) {
  return (
    <div className="flex flex-col gap-3 rounded-md border border-border bg-muted/40 px-3 py-2.5 text-[12.5px]">
      {summary.already_imported ? (
        <p className="text-foreground">
          This file was already imported — nothing new to add.
        </p>
      ) : (
        <p className="text-foreground">
          Imported <span className="font-medium">{summary.txns_imported}</span>{" "}
          transaction{summary.txns_imported === 1 ? "" : "s"} (
          <span className="font-medium">{summary.instruments_new}</span> new
          instrument{summary.instruments_new === 1 ? "" : "s"})
          {summary.txns_skipped_dupe > 0
            ? `, skipped ${summary.txns_skipped_dupe} duplicate${
                summary.txns_skipped_dupe === 1 ? "" : "s"
              }`
            : ""}
          {summary.rows_rejected > 0
            ? `, rejected ${summary.rows_rejected} row${
                summary.rows_rejected === 1 ? "" : "s"
              }`
            : ""}
          .
        </p>
      )}

      {summary.warnings.length > 0 ? (
        <div className="flex flex-col gap-1">
          <p className="font-medium text-foreground">
            {summary.warnings.length} row
            {summary.warnings.length === 1 ? "" : "s"} skipped:
          </p>
          <ul className="ml-3 list-disc text-muted-foreground">
            {summary.warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <p className="text-muted-foreground">
        Current value and P&amp;L stay blank until you set each instrument’s
        NAV.{" "}
        <Link
          href="/holdings"
          className="font-medium text-primary hover:underline"
        >
          View holdings
        </Link>{" "}
        ·{" "}
        <Link
          href="/portfolio"
          className="font-medium text-primary hover:underline"
        >
          View portfolio
        </Link>
      </p>
    </div>
  );
}
