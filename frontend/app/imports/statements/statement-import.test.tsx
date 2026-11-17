import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { StatementImport } from "./statement-import";
import { AuthProvider } from "@/components/auth/auth-provider";
import { buildImportSummary } from "@/tests/helpers/factories";
import { renderWithProviders, screen, waitFor } from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";
import { mockUser } from "@/tests/mocks/handlers";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
  useSearchParams: () => new URLSearchParams(),
}));

function asGuest() {
  server.use(
    http.get("*/api/v1/auth/me", () => HttpResponse.json({ ...mockUser, is_guest: true, can_manage_credentials: false })),
  );
}

function renderPage() {
  return renderWithProviders(
    <AuthProvider>
      <StatementImport />
    </AuthProvider>,
  );
}

describe("StatementImport (app/imports/statements/statement-import.tsx)", () => {
  it("offers a guest the sample statements and no upload", async () => {
    asGuest();
    renderPage();

    expect(await screen.findByText("Axis Bank credit card, June 2025")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Import sample" })).toBeInTheDocument();
    expect(screen.queryByText(/Upload statement/i)).not.toBeInTheDocument();
    expect(document.querySelector('input[type="file"]')).toBeNull();
  });

  it("imports a sample and opens its review queue", async () => {
    asGuest();
    let body: unknown = null;
    server.use(
      http.post("*/api/v1/imports/samples", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(
          buildImportSummary({
            batch_id: 5,
            imported: 12,
            pending_count: 12,
            reconciliation_delta_paise: 0,
          }),
        );
      }),
    );
    const { user } = renderPage();

    await user.click(await screen.findByRole("button", { name: "Import sample" }));

    await waitFor(() => expect(push).toHaveBeenCalledWith("/imports/review/5"));
    expect(body).toEqual({ sample_id: "axis-cc-2025-06", account_id: 2 });
  });

  it("offers a signed-in user the upload form", async () => {
    renderPage();

    await waitFor(() =>
      expect(document.querySelector('input[type="file"]')).not.toBeNull(),
    );
    expect(screen.queryByRole("button", { name: "Import sample" })).not.toBeInTheDocument();
  });
});
