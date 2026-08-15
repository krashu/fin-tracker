import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { RefreshPricesButton } from "./refresh-prices-button";
import { renderWithProviders, screen, waitFor } from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";
import {
  mockBenchmarkRefreshSummary,
  mockNavRefreshSummary,
} from "@/tests/mocks/handlers";

describe("RefreshPricesButton (components/refresh-prices-button.tsx)", () => {
  it("renders refresh button with initial accessible label", () => {
    renderWithProviders(<RefreshPricesButton />);
    expect(
      screen.getByRole("button", { name: "Refresh prices" }),
    ).toBeInTheDocument();
  });

  it("handles successful full refresh (scope='all') and invalidates query caches", async () => {
    let navCalled = false;
    let benchCalled = false;

    server.use(
      http.post("*/api/v1/instruments/refresh-navs", () => {
        navCalled = true;
        return HttpResponse.json(mockNavRefreshSummary);
      }),
      http.post("*/api/v1/benchmarks/refresh", () => {
        benchCalled = true;
        return HttpResponse.json(mockBenchmarkRefreshSummary);
      }),
    );

    const { user, queryClient } = renderWithProviders(<RefreshPricesButton scope="all" />);
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    const button = screen.getByRole("button", { name: "Refresh prices" });
    await user.click(button);

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent("Prices updated.");
    });

    expect(navCalled).toBe(true);
    expect(benchCalled).toBe(true);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["holdings"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["dashboards"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["portfolio"] });
  });

  it("triggers only nav refresh when scope='navs'", async () => {
    let navCalled = false;
    let benchCalled = false;

    server.use(
      http.post("*/api/v1/instruments/refresh-navs", () => {
        navCalled = true;
        return HttpResponse.json(mockNavRefreshSummary);
      }),
      http.post("*/api/v1/benchmarks/refresh", () => {
        benchCalled = true;
        return HttpResponse.json(mockBenchmarkRefreshSummary);
      }),
    );

    const { user } = renderWithProviders(<RefreshPricesButton scope="navs" />);

    const button = screen.getByRole("button", { name: "Refresh prices" });
    await user.click(button);

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent("Prices updated.");
    });

    expect(navCalled).toBe(true);
    expect(benchCalled).toBe(false);
  });

  it("displays alert panel when partial failures/warnings occur", async () => {
    server.use(
      http.post("*/api/v1/instruments/refresh-navs", () => {
        return HttpResponse.json({
          ...mockNavRefreshSummary,
          fetch_errors: 1,
          warnings: ["AMFI: Scheme 120503 NAV unavailable"],
        });
      }),
      http.post("*/api/v1/benchmarks/refresh", () => {
        return HttpResponse.json(mockBenchmarkRefreshSummary);
      }),
    );

    const { user } = renderWithProviders(<RefreshPricesButton scope="all" />);

    const refreshBtn = screen.getByRole("button", { name: "Refresh prices" });
    await user.click(refreshBtn);

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent(
        "Some prices couldn’t be refreshed.",
      );
    });

    const alertBtn = screen.getByRole("button", {
      name: "Some prices couldn’t be refreshed — view details",
    });
    expect(alertBtn).toBeInTheDocument();

    await user.click(alertBtn);

    expect(screen.getByText("Holdings NAVs")).toBeInTheDocument();
    expect(
      screen.getByText("AMFI: Scheme 120503 NAV unavailable"),
    ).toBeInTheDocument();
  });

  it("displays total failure state when API calls fail completely", async () => {
    server.use(
      http.post("*/api/v1/instruments/refresh-navs", () => {
        return HttpResponse.json({ detail: "Server error" }, { status: 500 });
      }),
      http.post("*/api/v1/benchmarks/refresh", () => {
        return HttpResponse.json({ detail: "Server error" }, { status: 500 });
      }),
    );

    const { user } = renderWithProviders(<RefreshPricesButton scope="all" />);

    const refreshBtn = screen.getByRole("button", { name: "Refresh prices" });
    await user.click(refreshBtn);

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent(
        "Couldn’t refresh prices — the API is unreachable.",
      );
    });

    const alertBtn = screen.getByRole("button", {
      name: "Refresh failed — view details",
    });
    expect(alertBtn).toBeInTheDocument();

    await user.click(alertBtn);
    expect(
      screen.getByText("Couldn’t reach the API — is the backend running?"),
    ).toBeInTheDocument();
  });
});
