import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { TaggingHealthCard } from "./tagging-health-card";
import { renderWithProviders, screen } from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";
import type { TaggingStatsResponse } from "@/lib/api/client";

describe("TaggingHealthCard (components/dashboard/tagging-health-card.tsx)", () => {
  it("renders auto-tag accuracy and coverage metrics", async () => {
    const stats: TaggingStatsResponse = {
      total_auto_tagged: 45,
      kept: 40,
      acceptance_rate: 0.89,
      rules_count: 12,
      imported_total: 50,
      pre_tagged: 45,
      coverage_rate: 0.9,
    };

    server.use(
      http.get("*/api/v1/dashboards/tagging-stats", () => {
        return HttpResponse.json(stats);
      }),
    );

    renderWithProviders(<TaggingHealthCard />);

    expect(await screen.findByText("Auto-tag accuracy")).toBeInTheDocument();
    expect(await screen.findByText("89%")).toBeInTheDocument();
    expect(
      screen.getByText("45 of 50 imported rows pre-tagged (90%, target ≥ 80%)"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("40 of 45 auto-tagged rows kept · 12 learned rules"),
    ).toBeInTheDocument();
  });

  it("handles zero imports state", async () => {
    const stats: TaggingStatsResponse = {
      total_auto_tagged: 0,
      kept: 0,
      acceptance_rate: null,
      rules_count: 0,
      imported_total: 0,
      pre_tagged: 0,
      coverage_rate: null,
    };

    server.use(
      http.get("*/api/v1/dashboards/tagging-stats", () => {
        return HttpResponse.json(stats);
      }),
    );

    renderWithProviders(<TaggingHealthCard />);

    expect(await screen.findByText("—")).toBeInTheDocument();
    expect(screen.getByText("No imports yet.")).toBeInTheDocument();
    expect(screen.getByText("No auto-tagged imports yet.")).toBeInTheDocument();
  });

  it("handles API error state gracefully", async () => {
    server.use(
      http.get("*/api/v1/dashboards/tagging-stats", () => {
        return HttpResponse.json({ detail: "Server error" }, { status: 500 });
      }),
    );

    renderWithProviders(<TaggingHealthCard />);

    expect(
      await screen.findByText("Couldn’t load — is the API running?"),
    ).toBeInTheDocument();
  });
});
