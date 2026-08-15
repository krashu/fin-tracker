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
});
