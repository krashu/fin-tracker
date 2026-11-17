import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { SecurityManager } from "./security-manager";
import { AuthProvider } from "@/components/auth/auth-provider";
import { renderWithProviders, screen, waitFor } from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";
import { buildAuthUser } from "@/tests/helpers/factories";

function renderManager() {
  return renderWithProviders(
    <AuthProvider>
      <SecurityManager />
    </AuthProvider>,
  );
}

describe("SecurityManager (change password, ADR-0003)", () => {
  it("shows a notice instead of the change-password form when the account cannot manage credentials", async () => {
    server.use(
      http.get("*/api/v1/auth/me", () =>
        HttpResponse.json(buildAuthUser({ is_guest: false, can_manage_credentials: false })),
      ),
    );
    const { queryClient } = renderManager();
    // The notice also shows while /me is in flight; assert only once the user has loaded.
    await waitFor(() => expect(queryClient.getQueryData(["auth", "me"])).toBeTruthy());

    expect(await screen.findByText(/no password to change/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Current password")).not.toBeInTheDocument();
  });

  it("shows neither the form nor the notice while /auth/me is still loading", () => {
    server.use(http.get("*/api/v1/auth/me", () => new Promise<never>(() => {})));
    const { container } = renderManager();

    expect(screen.queryByText(/no password to change/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Current password")).not.toBeInTheDocument();
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the change-password form to an account that manages its credentials", async () => {
    renderManager();

    expect(await screen.findByLabelText("Current password")).toBeInTheDocument();
    expect(screen.queryByText(/no password to change/)).not.toBeInTheDocument();
  });
});
