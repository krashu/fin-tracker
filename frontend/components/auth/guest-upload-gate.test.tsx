import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { GuestUploadGate } from "./guest-upload-gate";
import { AuthProvider } from "@/components/auth/auth-provider";
import { renderWithProviders, screen } from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";
import { mockUser } from "@/tests/mocks/handlers";

function renderGate() {
  return renderWithProviders(
    <AuthProvider>
      <GuestUploadGate>
        <button type="button">Upload</button>
      </GuestUploadGate>
    </AuthProvider>,
  );
}

describe("GuestUploadGate (components/auth/guest-upload-gate.tsx)", () => {
  it("hides the upload action from a guest", async () => {
    server.use(
      http.get("*/api/v1/auth/me", () => HttpResponse.json({ ...mockUser, is_guest: true, can_manage_credentials: false })),
    );
    renderGate();

    expect(await screen.findByText(/Guests can’t upload files/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Upload" })).not.toBeInTheDocument();
  });

  it("renders the children for a signed-in user", async () => {
    renderGate();

    expect(await screen.findByRole("button", { name: "Upload" })).toBeInTheDocument();
  });

  it("renders nothing while authenticating", () => {
    server.use(http.get("*/api/v1/auth/me", () => new Promise<never>(() => {})));
    const { container } = renderGate();

    expect(container).toBeEmptyDOMElement();
  });
});
