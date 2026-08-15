import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { AliasManager } from "./alias-manager";
import { renderWithProviders, screen, waitFor } from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";
import { buildMerchantAlias } from "@/tests/helpers/factories";

describe("AliasManager (app/settings/rules/alias-manager.tsx)", () => {
  const initialAliases = [
    buildMerchantAlias({ id: 1, pattern: "swiggy india", canonical: "swiggy", is_seeded: false }),
    buildMerchantAlias({ id: 2, pattern: "amzn in", canonical: "amazon", is_seeded: true }),
  ];

  it("renders list of existing aliases and counts", async () => {
    server.use(
      http.get("*/api/v1/rules/aliases", () => {
        return HttpResponse.json(initialAliases);
      }),
    );

    renderWithProviders(<AliasManager />);

    expect(await screen.findByText("2 aliases")).toBeInTheDocument();
    expect(screen.getByText("swiggy india")).toBeInTheDocument();
    expect(screen.getByText("swiggy")).toBeInTheDocument();
    expect(screen.getByText("amzn in")).toBeInTheDocument();
    expect(screen.getByText("amazon")).toBeInTheDocument();
  });

  it("creates a new merchant alias and invalidates rules cache", async () => {
    let createdPayload: { pattern: string; canonical: string } | null = null;

    server.use(
      http.get("*/api/v1/rules/aliases", () => {
        return HttpResponse.json(initialAliases);
      }),
      http.post("*/api/v1/rules/aliases", async ({ request }) => {
        createdPayload = (await request.json()) as { pattern: string; canonical: string };
        return HttpResponse.json({
          id: 10,
          pattern: createdPayload.pattern,
          canonical: createdPayload.canonical,
          is_seeded: false,
        });
      }),
    );

    const { user, queryClient } = renderWithProviders(<AliasManager />);
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    const newBtn = await screen.findByRole("button", { name: "New alias" });
    await user.click(newBtn);

    expect(screen.getByRole("heading", { name: "New alias" })).toBeInTheDocument();

    const patternInput = screen.getByPlaceholderText("e.g. swiggy blr");
    const canonicalInput = screen.getByPlaceholderText("e.g. Swiggy");

    await user.type(patternInput, "uber trip");
    await user.type(canonicalInput, "uber");

    const addBtn = screen.getByRole("button", { name: "Add alias" });
    await user.click(addBtn);

    await waitFor(() => {
      expect(createdPayload).toEqual({
        pattern: "uber trip",
        canonical: "uber",
      });
    });

    expect(
      screen.getByText(/Added alias for “uber trip”\. Add another, or Done\./),
    ).toBeInTheDocument();

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["rules"] });
  });

  it("deletes an alias and invalidates rules cache (ADR-0011)", async () => {
    let deletedId: number | null = null;

    server.use(
      http.get("*/api/v1/rules/aliases", () => {
        return HttpResponse.json(initialAliases);
      }),
      http.delete("*/api/v1/rules/aliases/:id", ({ params }) => {
        deletedId = Number(params.id);
        return new HttpResponse(null, { status: 204 });
      }),
    );

    const { user, queryClient } = renderWithProviders(<AliasManager />);
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    const menuBtn = await screen.findByRole("button", {
      name: "Actions for swiggy india",
    });
    await user.click(menuBtn);

    const deleteOption = await screen.findByText("Delete alias");
    await user.click(deleteOption);

    expect(
      screen.getByRole("heading", { name: /Delete alias “swiggy india” → “swiggy”\?/ }),
    ).toBeInTheDocument();

    const confirmBtn = screen.getByRole("button", { name: "Delete" });
    await user.click(confirmBtn);

    await waitFor(() => {
      expect(deletedId).toBe(1);
    });

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["rules"] });
  });
});
