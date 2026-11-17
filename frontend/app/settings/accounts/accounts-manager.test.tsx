import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { AccountsManager } from "./accounts-manager";
import { buildAccount } from "@/tests/helpers/factories";
import {
  createTestQueryClient,
  renderWithProviders,
  screen,
  waitFor,
} from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";

async function openCreateDialog(user: ReturnType<typeof renderWithProviders>["user"]) {
  await user.click(await screen.findByRole("button", { name: "New account" }));
  await screen.findByRole("heading", { name: /New account/ });
}

async function pickType(
  user: ReturnType<typeof renderWithProviders>["user"],
  label: string,
) {
  await user.click(screen.getByRole("button", { name: /Credit card/ }));
  await user.click(await screen.findByRole("menuitem", { name: label }));
}

describe("AccountsManager issuer pickers (PRD §F6)", () => {
  it("lists the server's brand label for a stored issuer", async () => {
    server.use(
      http.get("*/api/v1/accounts", () =>
        HttpResponse.json([
          buildAccount({ id: 1, name: "Salary", type: "bank", issuer: "hdfc" }),
        ]),
      ),
    );

    renderWithProviders(<AccountsManager />);

    expect(await screen.findByText("HDFC")).toBeInTheDocument();
    expect(screen.queryByText("hdfc")).not.toBeInTheDocument();
  });

  it("offers None and the server's bank issuers for a bank, and posts the canonical id", async () => {
    let posted: Record<string, unknown> | null = null;
    server.use(
      http.post("*/api/v1/accounts", async ({ request }) => {
        posted = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          buildAccount({ id: 50, name: "My Savings", issuer: "hdfc" }),
          { status: 201 },
        );
      }),
    );

    const { user } = renderWithProviders(<AccountsManager />);
    await openCreateDialog(user);
    await pickType(user, "Bank");
    await user.type(screen.getByRole("textbox", { name: /Name/ }), "My Savings");

    await user.click(screen.getByRole("button", { name: /None/ }));
    expect(await screen.findByRole("menuitem", { name: "None" })).toBeInTheDocument();
    await user.click(await screen.findByRole("menuitem", { name: "HDFC" }));

    await user.click(screen.getByRole("button", { name: /^(Create|Add|Save)/ }));

    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).toMatchObject({ type: "bank", issuer: "hdfc" });
  });

  it("lists the server's card issuers for a credit card", async () => {
    const { user } = renderWithProviders(<AccountsManager />);
    await openCreateDialog(user);

    await user.click(screen.getByRole("button", { name: /Select issuer/ }));

    expect(await screen.findByRole("menuitem", { name: "Axis" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "ICICI" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "HDFC" })).not.toBeInTheDocument();
  });

  it("clears the issuer when the type changes, since each type has its own list", async () => {
    const { user } = renderWithProviders(<AccountsManager />);
    await openCreateDialog(user);
    await user.click(screen.getByRole("button", { name: /Select issuer/ }));
    await user.click(await screen.findByRole("menuitem", { name: "Axis" }));

    await pickType(user, "Bank");

    expect(screen.getByRole("button", { name: /None/ })).toBeInTheDocument();
  });

  it("keeps a free-text issuer box for cash", async () => {
    const { user } = renderWithProviders(<AccountsManager />);
    await openCreateDialog(user);
    await pickType(user, "Cash");

    expect(screen.getByPlaceholderText("e.g. hdfc")).toBeInTheDocument();
  });
});

describe("AccountsManager review-queue invalidation (ADR-0002 rule 7b)", () => {
  it("refreshes review candidates after saving an account", async () => {
    const queryClient = createTestQueryClient();
    const { user } = renderWithProviders(<AccountsManager />, { queryClient });
    const spy = vi.spyOn(queryClient, "invalidateQueries");

    await user.click(
      await screen.findByRole("button", { name: "Edit ICICI Amazon Pay CC" }),
    );
    const name = await screen.findByRole("textbox", { name: /Name/ });
    await user.type(name, " 2");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith({ queryKey: ["candidates"] }),
    );
  });

  it("refreshes review candidates after archiving an account", async () => {
    const queryClient = createTestQueryClient();
    const { user } = renderWithProviders(<AccountsManager />, { queryClient });
    const spy = vi.spyOn(queryClient, "invalidateQueries");

    await user.click(
      await screen.findByRole("button", { name: "Archive ICICI Amazon Pay CC" }),
    );
    await user.click(await screen.findByRole("button", { name: "Archive" }));

    await waitFor(() =>
      expect(spy).toHaveBeenCalledWith({ queryKey: ["candidates"] }),
    );
  });
});
