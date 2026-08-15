import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { NewRuleDialog } from "./new-rule-dialog";
import { renderWithProviders, screen, waitFor } from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";
import { mockCategories, mockMerchants } from "@/tests/mocks/handlers";

describe("NewRuleDialog (app/settings/rules/new-rule-dialog.tsx)", () => {
  it("renders closed by default when open=false", () => {
    renderWithProviders(<NewRuleDialog open={false} onClose={vi.fn()} />);
    expect(screen.queryByText("New rule")).not.toBeInTheDocument();
  });

  it("renders dialog contents and disables Pin rule button initially", async () => {
    renderWithProviders(<NewRuleDialog open={true} onClose={vi.fn()} />);

    expect(screen.getByRole("heading", { name: "New rule" })).toBeInTheDocument();
    expect(
      screen.getByText(/Pin a category and\/or tags to a merchant/),
    ).toBeInTheDocument();

    const pinButton = screen.getByRole("button", { name: "Pin rule" });
    expect(pinButton).toBeDisabled();
  });

  it("submits category rule, executes mutation, and invalidates rules cache", async () => {
    let postCategoryPayload: { merchant: string; category_id: number } | null = null;

    server.use(
      http.get("*/api/v1/categories", () => {
        return HttpResponse.json(mockCategories);
      }),
      http.get("*/api/v1/rules/merchants", () => {
        return HttpResponse.json(mockMerchants);
      }),
      http.post("*/api/v1/rules/categories", async ({ request }) => {
        postCategoryPayload = (await request.json()) as {
          merchant: string;
          category_id: number;
        };
        return HttpResponse.json({
          id: 501,
          merchant_normalized: postCategoryPayload.merchant.toLowerCase(),
          pinned: true,
        });
      }),
    );

    const { user, queryClient } = renderWithProviders(
      <NewRuleDialog open={true} onClose={vi.fn()} />,
    );
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    // Open merchant dropdown and select "swiggy"
    const merchantTrigger = screen.getByRole("button", {
      name: /Search or type a merchant/,
    });
    await user.click(merchantTrigger);

    const merchantOption = await screen.findByText("swiggy");
    await user.click(merchantOption);

    // Open category selector and pick "Food & Dining"
    const categoryTrigger = screen.getByText("No category");
    await user.click(categoryTrigger);

    const categoryOption = await screen.findByText("Food & Dining");
    await user.click(categoryOption);

    // Submit rule
    const pinButton = screen.getByRole("button", { name: "Pin rule" });
    expect(pinButton).toBeEnabled();
    await user.click(pinButton);

    await waitFor(() => {
      expect(
        screen.getByText(/Pinned rule for “swiggy”\. Add another, or Done\./),
      ).toBeInTheDocument();
    });

    expect(postCategoryPayload).toEqual({
      merchant: "swiggy",
      category_id: 1,
    });

    // Invalidation contract verified
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["rules"] });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["dashboards", "tagging-stats"],
    });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["candidates"] });
  });

  it("displays server error message when pin rule fails", async () => {
    server.use(
      http.get("*/api/v1/categories", () => {
        return HttpResponse.json(mockCategories);
      }),
      http.get("*/api/v1/rules/merchants", () => {
        return HttpResponse.json(["amazon"]);
      }),
      http.post("*/api/v1/rules/categories", () => {
        return HttpResponse.json(
          { detail: "Rule already exists and is locked" },
          { status: 400 },
        );
      }),
    );

    const { user } = renderWithProviders(
      <NewRuleDialog open={true} onClose={vi.fn()} />,
    );

    const merchantTrigger = screen.getByRole("button", {
      name: /Search or type a merchant/,
    });
    await user.click(merchantTrigger);

    const merchantOption = await screen.findByText("amazon");
    await user.click(merchantOption);

    const categoryTrigger = screen.getByText("No category");
    await user.click(categoryTrigger);

    const categoryOption = await screen.findByText("Food & Dining");
    await user.click(categoryOption);

    const pinButton = screen.getByRole("button", { name: "Pin rule" });
    await user.click(pinButton);

    await waitFor(() => {
      expect(
        screen.getByText("Rule already exists and is locked"),
      ).toBeInTheDocument();
    });
  });
});
