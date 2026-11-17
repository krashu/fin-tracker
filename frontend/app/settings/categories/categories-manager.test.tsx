import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { CategoriesManager } from "./categories-manager";
import { renderWithProviders, screen, waitFor } from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";
import { buildCategory } from "@/tests/helpers/factories";
import type { CategoryCreate } from "@/lib/api/client";

describe("CategoriesManager (app/settings/categories/categories-manager.tsx)", () => {
  const initialCategories = [
    buildCategory({ id: 1, name: "Food & Dining", kind: "spend", parent_id: null, color: "#ef4444" }),
    buildCategory({ id: 2, name: "Groceries", kind: "spend", parent_id: 1, color: null }),
    buildCategory({ id: 3, name: "Salary", kind: "income", parent_id: null, color: "#22c55e" }),
  ];

  it("renders category hierarchy and counts", async () => {
    server.use(
      http.get("*/api/v1/categories", () => {
        return HttpResponse.json(initialCategories);
      }),
    );

    renderWithProviders(<CategoriesManager />);

    expect(await screen.findByText("3 categories")).toBeInTheDocument();
    expect(screen.getByText(/2 parent categories · 1 subcategories/)).toBeInTheDocument();
    expect(screen.getByText("Food & Dining")).toBeInTheDocument();
    expect(screen.getByText("Groceries")).toBeInTheDocument();
    expect(screen.getByText("Salary")).toBeInTheDocument();
  });

  it("creates a new root category and invalidates caches", async () => {
    let createdPayload: CategoryCreate | null = null;

    server.use(
      http.get("*/api/v1/categories", () => {
        return HttpResponse.json(initialCategories);
      }),
      http.post("*/api/v1/categories", async ({ request }) => {
        createdPayload = (await request.json()) as CategoryCreate;
        return HttpResponse.json({
          id: 50,
          name: createdPayload.name,
          kind: createdPayload.kind ?? "spend",
          is_seeded: false,
          archived_at: null,
          color: createdPayload.color ?? null,
          parent_id: createdPayload.parent_id ?? null,
          role: null,
        });
      }),
    );

    const { user, queryClient } = renderWithProviders(<CategoriesManager />);
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    // Click "New category"
    const newBtn = await screen.findByRole("button", { name: "New category" });
    await user.click(newBtn);

    expect(screen.getByRole("heading", { name: "New category" })).toBeInTheDocument();

    const nameInput = screen.getByPlaceholderText("e.g. Food & Dining");
    await user.type(nameInput, "Entertainment");
    expect(screen.getByText("A palette color is assigned when you save.")).toBeInTheDocument();

    const saveBtn = screen.getByRole("button", { name: "Create" });
    await user.click(saveBtn);

    await waitFor(() => {
      expect(createdPayload).not.toBeNull();
    });

    expect(createdPayload).toMatchObject({
      name: "Entertainment",
      kind: "spend",
      parent_id: null,
    });
    expect(createdPayload).not.toHaveProperty("color");

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["categories"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["dashboards"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["rules"] });
  });

  it("creates a subcategory under an existing parent", async () => {
    let createdPayload: CategoryCreate | null = null;

    server.use(
      http.get("*/api/v1/categories", () => {
        return HttpResponse.json(initialCategories);
      }),
      http.post("*/api/v1/categories", async ({ request }) => {
        createdPayload = (await request.json()) as CategoryCreate;
        return HttpResponse.json({
          id: 51,
          name: createdPayload.name,
          kind: createdPayload.kind ?? "spend",
          is_seeded: false,
          archived_at: null,
          color: null,
          parent_id: createdPayload.parent_id ?? null,
          role: null,
        });
      }),
    );

    const { user } = renderWithProviders(<CategoriesManager />);

    // Find the add subcategory button for "Food & Dining"
    const addSubBtn = await screen.findByRole("button", {
      name: "Add subcategory to Food & Dining",
    });
    await user.click(addSubBtn);

    const nameInput = screen.getByPlaceholderText("e.g. Groceries");
    await user.type(nameInput, "Restaurants");

    const saveBtn = screen.getByRole("button", { name: "Create" });
    await user.click(saveBtn);

    await waitFor(() => {
      expect(createdPayload).not.toBeNull();
    });

    expect(createdPayload).toMatchObject({
      name: "Restaurants",
      kind: "spend",
      parent_id: 1,
    });
    // ADR-0012 rule 7: a new subcategory defaults to NULL colour (inherits).
    expect(createdPayload).not.toHaveProperty("color");
  });

  it("offers no archive and no rename on the fallback (ADR-0012 rule 8)", async () => {
    server.use(
      http.get("*/api/v1/categories", () => {
        return HttpResponse.json([
          ...initialCategories,
          buildCategory({ id: 9, name: "Other", kind: "spend", role: "fallback" }),
        ]);
      }),
    );

    const { user } = renderWithProviders(<CategoriesManager />);

    // Food & Dining is archivable; the fallback is not, and renders no button.
    expect(
      await screen.findByRole("button", { name: "Archive Food & Dining" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Archive Other" })).toBeNull();

    await user.click(screen.getByRole("button", { name: "Edit Other" }));
    expect(screen.getByDisplayValue("Other")).toBeDisabled();
    expect(screen.getByText(/can.t be renamed/)).toBeInTheDocument();
    // Colour stays editable; reparenting does not exist for it.
    expect(screen.queryByRole("button", { name: "No custom color" })).toBeNull();
    expect((await screen.findAllByRole("button", { name: /^Color #/ }))[0]).toBeEnabled();
    expect(screen.queryByText("None (Top-level parent category)")).toBeNull();
  });

  it("offers the server's palette as swatches", async () => {
    server.use(
      http.get("*/api/v1/categories/palette", () =>
        HttpResponse.json({ colors: ["#111111", "#222222"] }),
      ),
    );
    const { user } = renderWithProviders(<CategoriesManager />);
    await user.click(await screen.findByRole("button", { name: "New category" }));

    expect(
      (await screen.findAllByRole("button", { name: /^Color #/ })).map((b) =>
        b.getAttribute("aria-label"),
      ),
    ).toEqual(["Color #111111", "Color #222222"]);
  });

  it("says when the palette cannot load", async () => {
    server.use(
      http.get("*/api/v1/categories/palette", () => new HttpResponse(null, { status: 500 })),
    );
    const { user } = renderWithProviders(<CategoriesManager />);
    await user.click(await screen.findByRole("button", { name: "New category" }));

    expect(await screen.findByText("Couldn’t load the color palette.")).toBeInTheDocument();
  });

  it("soft-deletes category and invalidates rule memory (ADR-0012)", async () => {
    let deletedId: number | null = null;

    server.use(
      http.get("*/api/v1/categories", () => {
        return HttpResponse.json(initialCategories);
      }),
      http.delete("*/api/v1/categories/:id", ({ params }) => {
        deletedId = Number(params.id);
        return new HttpResponse(null, { status: 204 });
      }),
    );

    const { user, queryClient } = renderWithProviders(<CategoriesManager />);
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    // Click archive button on Salary category row
    const archiveBtn = await screen.findByRole("button", {
      name: "Archive Salary",
    });
    await user.click(archiveBtn);

    // Confirm dialog
    expect(
      screen.getByRole("heading", { name: "Archive Salary?" }),
    ).toBeInTheDocument();

    const confirmBtn = screen.getByRole("button", { name: "Archive" });
    await user.click(confirmBtn);

    await waitFor(() => {
      expect(deletedId).toBe(3);
    });

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["categories"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["dashboards"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["rules"] });
  });

  describe("archive with reassign (ADR-0012 rule 10)", () => {
    const withOther = [
      ...initialCategories,
      buildCategory({ id: 4, name: "Other", kind: "spend", parent_id: null, color: "#888888" }),
    ];

    async function openArchiveFoodDialog() {
      const view = renderWithProviders(<CategoriesManager />);
      await view.user.click(
        await screen.findByRole("button", { name: "Archive Food & Dining" }),
      );
      return view;
    }

    it("archives with reassign and moves the transactions to the picked category", async () => {
      let deleteUrl: URL | null = null;
      server.use(
        http.get("*/api/v1/categories", () => HttpResponse.json(withOther)),
        http.delete("*/api/v1/categories/:id", ({ request }) => {
          deleteUrl = new URL(request.url);
          return new HttpResponse(null, { status: 204 });
        }),
      );
      const { user, queryClient } = await openArchiveFoodDialog();
      const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

      await user.click(screen.getByRole("button", { name: /Don't move them/ }));
      await user.click(await screen.findByRole("option", { name: /Other/ }));
      await user.click(screen.getByRole("button", { name: "Archive" }));

      await waitFor(() => expect(deleteUrl).not.toBeNull());
      expect(deleteUrl!.pathname).toMatch(/\/categories\/1$/);
      expect(deleteUrl!.searchParams.get("reassign_to")).toBe("4");
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["transactions"] });
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["categories"] });
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["rules"] });
    });

    it("offers only live same-kind categories outside the archived family as targets", async () => {
      server.use(http.get("*/api/v1/categories", () => HttpResponse.json(withOther)));
      const { user } = await openArchiveFoodDialog();

      await user.click(screen.getByRole("button", { name: /Don't move them/ }));
      const names = (await screen.findAllByRole("option")).map((o) => o.textContent);

      expect(names.some((n) => n?.includes("Other"))).toBe(true);
      expect(names.some((n) => n?.includes("Food & Dining"))).toBe(false);
      expect(names.some((n) => n?.includes("Groceries"))).toBe(false);
      expect(names.some((n) => n?.includes("Salary"))).toBe(false);
    });

    it("sends no reassign_to by default", async () => {
      let deleteUrl: URL | null = null;
      server.use(
        http.get("*/api/v1/categories", () => HttpResponse.json(withOther)),
        http.delete("*/api/v1/categories/:id", ({ request }) => {
          deleteUrl = new URL(request.url);
          return new HttpResponse(null, { status: 204 });
        }),
      );
      const { user } = await openArchiveFoodDialog();

      await user.click(screen.getByRole("button", { name: "Archive" }));

      await waitFor(() => expect(deleteUrl).not.toBeNull());
      expect(deleteUrl!.searchParams.has("reassign_to")).toBe(false);
    });
  });
});
