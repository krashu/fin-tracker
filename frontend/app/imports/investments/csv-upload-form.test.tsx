import { describe, expect, it } from "vitest";
import { CsvSummary, CsvUploadForm } from "./csv-upload-form";
import { renderWithProviders, screen } from "@/tests/helpers/test-utils";
import type { InvestmentCsvImportSummary } from "@/lib/api/client";

const summary = (
  over: Partial<InvestmentCsvImportSummary> = {},
): InvestmentCsvImportSummary => ({
  batch_id: 1,
  instruments_new: 1,
  txns_imported: 5,
  txns_skipped_dupe: 0,
  rows_rejected: 0,
  already_imported: false,
  warnings: [],
  ...over,
});

describe("CsvSummary (app/imports/investments/csv-upload-form.tsx)", () => {
  it("titles the list by warnings, not skipped rows, when rows outnumber warnings", () => {
    renderWithProviders(
      <CsvSummary
        summary={summary({ rows_rejected: 2, warnings: ["row 3: short"] })}
      />,
    );

    expect(screen.getByText(/rejected 2 rows/)).toBeInTheDocument();
    expect(screen.getByText("1 warning:")).toBeInTheDocument();
    expect(screen.queryByText(/skipped:/)).not.toBeInTheDocument();
  });

  it("pluralises the warning count", () => {
    renderWithProviders(
      <CsvSummary summary={summary({ warnings: ["a", "b", "c"] })} />,
    );

    expect(screen.getByText("3 warnings:")).toBeInTheDocument();
  });

  it("renders no warning list at zero", () => {
    renderWithProviders(<CsvSummary summary={summary()} />);

    expect(screen.queryByText(/warning/)).not.toBeInTheDocument();
  });
});

describe("CsvUploadForm currency note (B19)", () => {
  it("states the fixed-currency classes from the server's rule", async () => {
    renderWithProviders(<CsvUploadForm />);

    expect(await screen.findByText(/NPS rows in INR/)).toBeInTheDocument();
  });
});
