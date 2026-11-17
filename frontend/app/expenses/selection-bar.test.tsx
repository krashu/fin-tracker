import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { SelectionBar } from "./selection-bar";
import { renderWithProviders, screen, waitFor, within } from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";
import { buildCategory } from "@/tests/helpers/factories";
import { BULK_MAX } from "@/lib/api/client";

const categories = [
  buildCategory({ id: 1, name: "Food & Dining", kind: "spend" }),
  buildCategory({ id: 2, name: "Groceries", kind: "spend", parent_id: 1 }),
];

function renderBar(ids: number[], onDone = vi.fn()) {
  return {
    onDone,
    ...renderWithProviders(
      <SelectionBar
        ids={ids}
        categories={categories}
        categoryKind="spend"
        onClear={vi.fn()}
        onDone={onDone}
      />,
    ),
  };
}

describe("SelectionBar (app/expenses/selection-bar.tsx)", () => {
  it("categorises the whole selection with one bulk request", async () => {
    const bulkBodies: unknown[] = [];
    const patch = vi.fn();
    server.use(
      http.post("*/api/v1/transactions/bulk-categorise", async ({ request }) => {
        bulkBodies.push(await request.json());
        return HttpResponse.json({ updated: 3, taught: 1 });
      }),
      http.patch("*/api/v1/transactions/:id", () => {
        patch();
        return new HttpResponse(null, { status: 500 });
      }),
    );
    const { user, queryClient, onDone } = renderBar([1, 2, 3]);
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");

    await user.click(screen.getByRole("button", { name: "Categorize" }));
    await user.click(await screen.findByText("Groceries"));

    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(bulkBodies).toEqual([{ transaction_ids: [1, 2, 3], category_id: 2 }]);
    expect(patch).not.toHaveBeenCalled();
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["transactions"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["dashboards"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["rules"] });
  });

  it("reports invalid ids from a 422 and changes nothing", async () => {
    server.use(
      http.post("*/api/v1/transactions/bulk-categorise", () =>
        HttpResponse.json(
          { detail: { message: "bad ids", invalid_ids: [2] } },
          { status: 422 },
        ),
      ),
    );
    const { user, queryClient, onDone } = renderBar([1, 2, 3]);
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");

    await user.click(screen.getByRole("button", { name: "Categorize" }));
    await user.click(await screen.findByText("Groceries"));

    expect(
      await screen.findByText("Nothing changed: 1 of 3 rows can't be categorised."),
    ).toBeInTheDocument();
    expect(onDone).not.toHaveBeenCalled();
    // The board still refetches: the rows may have changed under the selection.
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["transactions"] });
  });

  it("refuses to categorise or delete more than 500 rows", () => {
    const ids = Array.from({ length: BULK_MAX + 1 }, (_, i) => i + 1);
    renderBar(ids);

    const trigger = screen.getByRole("button", { name: "Categorize" });
    expect(trigger).toBeDisabled();
    expect(trigger).toHaveAttribute("title", "Select at most 500 rows to categorise");
    const del = screen.getByRole("button", { name: "Delete" });
    expect(del).toBeDisabled();
    expect(del).toHaveAttribute("title", "Select at most 500 rows to delete");
  });

  it("deletes the whole selection with one bulk request", async () => {
    const bulkBodies: unknown[] = [];
    const single = vi.fn();
    server.use(
      http.post("*/api/v1/transactions/bulk-delete", async ({ request }) => {
        bulkBodies.push(await request.json());
        return new HttpResponse(null, { status: 204 });
      }),
      http.delete("*/api/v1/transactions/:id", () => {
        single();
        return new HttpResponse(null, { status: 500 });
      }),
    );
    const { user, queryClient, onDone } = renderBar([1, 2, 3]);
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");

    await user.click(screen.getByRole("button", { name: "Delete" }));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete" }),
    );

    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(bulkBodies).toEqual([{ transaction_ids: [1, 2, 3] }]);
    expect(single).not.toHaveBeenCalled();
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["transactions"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["dashboards"] });
  });

  it("reports invalid ids from a bulk-delete 422 and deletes nothing", async () => {
    server.use(
      http.post("*/api/v1/transactions/bulk-delete", () =>
        HttpResponse.json(
          { detail: { message: "bad ids", invalid_ids: [2] } },
          { status: 422 },
        ),
      ),
    );
    const { user, queryClient, onDone } = renderBar([1, 2, 3]);
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");

    await user.click(screen.getByRole("button", { name: "Delete" }));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete" }),
    );

    expect(
      await screen.findByText("Nothing deleted: 1 of 3 rows no longer exist."),
    ).toBeInTheDocument();
    expect(onDone).not.toHaveBeenCalled();
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["transactions"] });
  });
});
