import { describe, expect, it } from "vitest";
import { LoadSummary } from "./backup-manager";
import { renderWithProviders, screen } from "@/tests/helpers/test-utils";
import type { BackupImportSummary } from "@/lib/api/client";

const summary = (over: Partial<BackupImportSummary> = {}): BackupImportSummary => ({
  batch_id: 1,
  accounts_new: 0,
  accounts_matched: 0,
  categories_new: 0,
  categories_matched: 0,
  txns_imported: 5,
  txns_skipped_dupe: 0,
  rows_rejected: 0,
  links_not_restored: 0,
  transfers_relinked: 0,
  warnings: [],
  ...over,
});

describe("LoadSummary (app/settings/backup/backup-manager.tsx)", () => {
  it("reports rejected rows and links not restored as separate clauses", () => {
    renderWithProviders(
      <LoadSummary
        summary={summary({
          rows_rejected: 2,
          links_not_restored: 1,
          warnings: ["a", "b", "c"],
        })}
      />,
    );

    expect(screen.getByText(/rejected 2 rows/)).toBeInTheDocument();
    expect(screen.getByText(/1 link not restored/)).toBeInTheDocument();
    expect(screen.getByText("3 warnings:")).toBeInTheDocument();
  });

  it("renders neither clause at zero", () => {
    renderWithProviders(<LoadSummary summary={summary()} />);

    expect(screen.queryByText(/rejected/)).not.toBeInTheDocument();
    expect(screen.queryByText(/not restored/)).not.toBeInTheDocument();
    expect(screen.queryByText(/warning/)).not.toBeInTheDocument();
  });

  it("labels a single warning in the singular", () => {
    renderWithProviders(<LoadSummary summary={summary({ warnings: ["x"] })} />);

    expect(screen.getByText("1 warning:")).toBeInTheDocument();
  });
});
