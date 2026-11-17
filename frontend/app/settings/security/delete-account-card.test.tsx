import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import { DeleteAccountCard } from "./delete-account-card";
import { AuthProvider } from "@/components/auth/auth-provider";
import { renderWithProviders, screen, waitFor } from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";
import { buildAuthUser } from "@/tests/helpers/factories";
import type { AuthUser } from "@/lib/api/client";

const { replace } = vi.hoisted(() => ({ replace: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));

const PHRASE = "delete my account";

function meAs(user: AuthUser) {
  server.use(http.get("*/api/v1/auth/me", () => HttpResponse.json(user)));
}

async function renderCard() {
  const utils = renderWithProviders(
    <AuthProvider>
      <DeleteAccountCard />
    </AuthProvider>,
  );
  const user = userEvent.setup({ delay: null });
  await screen.findByLabelText("Current password");
  return { ...utils, user };
}

function passwordField() {
  return screen.getByLabelText("Current password");
}
function phraseField() {
  return screen.getByLabelText(/to confirm/);
}
function deleteButton() {
  return screen.getByRole("button", { name: "Delete my account" });
}

describe("DeleteAccountCard (ADR-0003 rule 13b)", () => {
  beforeEach(() => replace.mockClear());

  it("keeps Delete disabled until the password is typed and the phrase matches exactly", async () => {
    const { user } = await renderCard();
    expect(deleteButton()).toBeDisabled();

    await user.type(passwordField(), "pw-123456");
    await user.type(phraseField(), "Delete my account");
    expect(deleteButton()).toBeDisabled();

    await user.clear(phraseField());
    await user.type(phraseField(), PHRASE);
    expect(deleteButton()).toBeEnabled();
  });

  it("posts the password and the phrase, then signs the session out", async () => {
    let body: unknown = null;
    server.use(
      http.post("*/api/v1/auth/delete-account", async ({ request }) => {
        body = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { user, queryClient } = await renderCard();

    await user.type(passwordField(), "pw-123456");
    await user.type(phraseField(), PHRASE);
    await user.click(deleteButton());

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
    expect(body).toEqual({ password: "pw-123456", confirmation: PHRASE });
    expect(queryClient.getQueryData(["auth", "me"])).toBeNull();
  });

  it("shows the server's reason on a wrong password and stays signed in", async () => {
    server.use(
      http.post("*/api/v1/auth/delete-account", () =>
        HttpResponse.json({ detail: "current password is incorrect" }, { status: 400 }),
      ),
    );
    const { user, queryClient } = await renderCard();

    await user.type(passwordField(), "wrong-password");
    await user.type(phraseField(), PHRASE);
    await user.click(deleteButton());

    expect(await screen.findByText("current password is incorrect")).toBeInTheDocument();
    expect(queryClient.getQueryData<AuthUser>(["auth", "me"])?.id).toBe("u-test-1");
    expect(replace).not.toHaveBeenCalled();
  });

  it("offers a backup download before deleting", async () => {
    let hit = false;
    server.use(
      http.get("*/api/v1/backup", () => {
        hit = true;
        return new HttpResponse(new Blob(["zip"]), {
          headers: { "content-disposition": 'attachment; filename="b.zip"' },
        });
      }),
    );
    const createObjectURL = vi.fn(() => "blob:x");
    const revokeObjectURL = vi.fn();
    const original = { c: URL.createObjectURL, r: URL.revokeObjectURL };
    URL.createObjectURL = createObjectURL;
    URL.revokeObjectURL = revokeObjectURL;
    try {
      const { user } = await renderCard();
      await user.click(screen.getByRole("button", { name: "Download backup" }));
      await waitFor(() => expect(hit).toBe(true));
      await waitFor(() => expect(createObjectURL).toHaveBeenCalled());
    } finally {
      URL.createObjectURL = original.c;
      URL.revokeObjectURL = original.r;
    }
  });

  it("shows neither the form nor the notice while /auth/me is still loading", () => {
    server.use(http.get("*/api/v1/auth/me", () => new Promise<never>(() => {})));
    const { container } = renderWithProviders(
      <AuthProvider>
        <DeleteAccountCard />
      </AuthProvider>,
    );

    expect(screen.queryByText(/can't be deleted here/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Current password")).not.toBeInTheDocument();
    expect(container).toBeEmptyDOMElement();
  });

  it("shows a notice instead of the form when the account cannot manage credentials", async () => {
    // Demo-shaped: not a guest, yet the server says no. Proves nothing matches on is_guest.
    meAs(buildAuthUser({ is_guest: false, can_manage_credentials: false }));
    const { queryClient } = renderWithProviders(
      <AuthProvider>
        <DeleteAccountCard />
      </AuthProvider>,
    );
    // The notice also shows while /me is in flight; assert only once the user has loaded.
    await waitFor(() => expect(queryClient.getQueryData(["auth", "me"])).toBeTruthy());

    expect(await screen.findByText(/can't be deleted here/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Current password")).not.toBeInTheDocument();
  });
});
