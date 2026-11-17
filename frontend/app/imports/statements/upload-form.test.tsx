import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { UploadForm, UploadResult } from "./upload-form";
import { buildAccount, buildImportSummary } from "@/tests/helpers/factories";
import { renderWithProviders, screen } from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

describe("UploadForm account picker (PRD §F1)", () => {
  it("offers exactly the accounts the server says have a statement parser", async () => {
    server.use(
      http.get("*/api/v1/accounts", () =>
        HttpResponse.json([
          buildAccount({
            id: 1,
            name: "HDFC Savings",
            type: "bank",
            issuer: "hdfc",
            statement_import_supported: true,
          }),
          buildAccount({
            id: 2,
            name: "Axis Card",
            type: "credit_card",
            issuer: "axis",
            statement_import_supported: true,
          }),
          buildAccount({
            id: 3,
            name: "Kotak Savings",
            type: "bank",
            issuer: null,
            statement_import_supported: false,
          }),
          buildAccount({
            id: 4,
            name: "Wallet",
            type: "cash",
            issuer: null,
            statement_import_supported: false,
          }),
        ]),
      ),
    );

    const { user } = renderWithProviders(<UploadForm />);
    await user.click(
      await screen.findByRole("button", { name: /Select an account/ }),
    );

    expect(
      await screen.findByRole("menuitem", { name: /HDFC Savings/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("menuitem", { name: /Axis Card/ }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Kotak Savings/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Wallet/)).not.toBeInTheDocument();
  });

  it("explains what is needed when no account can import statements", async () => {
    server.use(
      http.get("*/api/v1/accounts", () =>
        HttpResponse.json([
          buildAccount({
            id: 3,
            name: "Kotak Savings",
            type: "bank",
            issuer: null,
            statement_import_supported: false,
          }),
        ]),
      ),
    );

    renderWithProviders(<UploadForm />);

    expect(
      await screen.findByText(/needs a card or bank account we can read/),
    ).toBeInTheDocument();
  });
});

describe("UploadResult (app/imports/statements/upload-form.tsx)", () => {
  it("says a 0-row statement has no transactions, not that they are already in expenses", () => {
    // ADR-0010 rule 5: a statement with no rows imports as an empty batch.
    renderWithProviders(<UploadResult summary={buildImportSummary()} />);

    expect(screen.getByText("This statement has no transactions.")).toBeInTheDocument();
    expect(screen.queryByText(/already in your/)).toBeNull();
  });

  it("says a statement of only zero-amount lines has no transactions, not that they are already in expenses", () => {
    // ADR-0010 rule 5: zero-amount lines are counted apart from duplicates.
    renderWithProviders(
      <UploadResult summary={buildImportSummary({ zero_amount_count: 3 })} />,
    );

    expect(screen.getByText("This statement has no transactions.")).toBeInTheDocument();
    expect(screen.getByText(/3 zero-amount lines were left out/)).toBeInTheDocument();
    expect(screen.queryByText(/already in your/)).toBeNull();
  });

  it("names zero-amount lines beside already-imported ones", () => {
    renderWithProviders(
      <UploadResult summary={buildImportSummary({ skipped: 2, zero_amount_count: 1 })} />,
    );

    expect(
      screen.getByText("Nothing new to import from this statement."),
    ).toBeInTheDocument();
    expect(screen.getByText(/already in your/)).toBeInTheDocument();
    expect(screen.getByText(/1 zero-amount line was left out/)).toBeInTheDocument();
  });

  it("says every row is already in expenses when rows were skipped", () => {
    renderWithProviders(<UploadResult summary={buildImportSummary({ skipped: 4 })} />);

    expect(
      screen.getByText("Nothing new to import from this statement."),
    ).toBeInTheDocument();
    expect(screen.getByText(/already in your/)).toBeInTheDocument();
  });

  it("says a re-upload was already imported", () => {
    renderWithProviders(
      <UploadResult summary={buildImportSummary({ already_imported: true, skipped: 4 })} />,
    );

    expect(screen.getByText("This statement was already imported.")).toBeInTheDocument();
  });
});
