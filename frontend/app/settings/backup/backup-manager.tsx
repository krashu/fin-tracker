"use client";

/**
 * Backup download + additive restore (PRD §F10). "Download backup" streams a zip of confirmed
 * spend transactions plus the accounts/categories they reference; "Load from backup" imports
 * one additively — rows already present are skipped by fingerprint, so re-loading after adding
 * more transactions just tops up and nothing is overwritten. On a successful load we invalidate
 * the shared queries so the board, filters, and dashboards reflect the new rows (PRD §F9).
 */
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { IconArchive, IconDoc, IconUpload } from "@/components/icons";
import { Field } from "@/components/form/fields";
import {
  ApiError,
  downloadBackup,
  importBackup,
  type BackupImportSummary,
} from "@/lib/api/client";
import { cn } from "@/lib/utils";

export function BackupManager() {
  const queryClient = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [summary, setSummary] = useState<BackupImportSummary | null>(null);

  const download = useMutation({
    mutationFn: downloadBackup,
    onSuccess: ({ blob, filename }) => {
      // Trigger the browser save from the returned blob (the client wrapper stays DOM-free).
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    },
  });

  const load = useMutation({
    mutationFn: (f: File) => importBackup(f),
    onSuccess: (s) => {
      setSummary(s);
      // An additive import can create accounts/categories and add transactions, so the shared
      // caches are now stale (PRD §F9). Skip the fan-out when nothing actually changed.
      if (s.txns_imported > 0 || s.accounts_new > 0 || s.categories_new > 0) {
        queryClient.invalidateQueries({ queryKey: ["transactions"] });
        queryClient.invalidateQueries({ queryKey: ["accounts"] });
        queryClient.invalidateQueries({ queryKey: ["categories"] });
        queryClient.invalidateQueries({ queryKey: ["dashboards"] });
        // Restored transactions can get-or-create labels; refresh the catalog.
        queryClient.invalidateQueries({ queryKey: ["labels"] });
      }
    },
  });

  function handleLoad() {
    if (file == null) return;
    setSummary(null);
    load.mutate(file);
  }

  return (
    <div className="flex flex-col gap-5">
      <Card>
        <CardHeader>
          <CardTitle>Download backup</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <p className="text-[12.5px] text-muted-foreground">
            A zip of your confirmed spend transactions plus the accounts and
            categories they use. Opens in Excel; keep it somewhere safe.
          </p>
          <div>
            <Button
              type="button"
              onClick={() => download.mutate()}
              disabled={download.isPending}
              className="h-9 gap-1.5 px-3.5 text-[12.5px] font-medium"
            >
              <IconArchive className="size-3.5" />
              {download.isPending ? "Preparing…" : "Download backup"}
            </Button>
          </div>
          {download.isError ? (
            <p className="text-[12px] text-neg">
              {download.error instanceof ApiError
                ? download.error.detail
                : "Couldn’t download — try again."}
            </p>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Load from backup</CardTitle>
        </CardHeader>
        <CardContent className="flex max-w-xl flex-col gap-4">
          <p className="text-[12.5px] text-muted-foreground">
            Import a backup zip. It’s additive — transactions already present
            are skipped, so nothing is overwritten. Safe to load into your
            current data or a fresh setup.
          </p>
          <Field label="Backup zip">
            {/* sr-only (not hidden) keeps the file input in the tab order + a11y tree;
                the visible label is the button and shows the focus ring via has-[]. */}
            <label className="flex cursor-pointer items-center gap-2 rounded-md border border-border bg-background px-2.5 py-2 text-[12.5px] transition-colors hover:bg-muted/50 has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-ring">
              <input
                type="file"
                accept=".zip,application/zip"
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
                {file ? file.name : "Choose a backup…"}
              </span>
            </label>
          </Field>

          {load.isError ? (
            <p className="text-[12px] text-neg">
              {load.error instanceof ApiError
                ? load.error.detail
                : "Couldn’t load — try again."}
            </p>
          ) : null}

          {summary ? <LoadSummary summary={summary} /> : null}

          <div>
            <Button
              type="button"
              onClick={handleLoad}
              disabled={file == null || load.isPending}
              className="h-9 gap-1.5 px-3.5 text-[12.5px] font-medium"
            >
              <IconUpload className="size-3.5" />
              {load.isPending ? "Loading…" : "Load backup"}
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function LoadSummary({ summary }: { summary: BackupImportSummary }) {
  return (
    <div className="flex flex-col gap-3 rounded-md border border-border bg-muted/40 px-3 py-2.5 text-[12.5px]">
      <p className="text-foreground">
        Added <span className="font-medium">{summary.txns_imported}</span>{" "}
        transaction{summary.txns_imported === 1 ? "" : "s"}
        {summary.txns_skipped_dupe > 0
          ? `, skipped ${summary.txns_skipped_dupe} already present`
          : ""}
        {summary.accounts_new > 0
          ? `, ${summary.accounts_new} new account${summary.accounts_new === 1 ? "" : "s"}`
          : ""}
        {summary.categories_new > 0
          ? `, ${summary.categories_new} new categor${summary.categories_new === 1 ? "y" : "ies"}`
          : ""}
        {summary.rows_rejected > 0
          ? `, rejected ${summary.rows_rejected} row${summary.rows_rejected === 1 ? "" : "s"}`
          : ""}
        .
      </p>

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
    </div>
  );
}
