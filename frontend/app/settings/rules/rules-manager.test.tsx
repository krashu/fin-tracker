import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { RulesManager } from "./rules-manager";
import { renderWithProviders, screen } from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";
import { buildMerchantRule } from "@/tests/helpers/factories";

describe("RulesManager (app/settings/rules/rules-manager.tsx)", () => {
  it("renders each rule group under its canonical merchant", async () => {
    server.use(
      http.get("*/api/v1/rules", () =>
        HttpResponse.json([buildMerchantRule({ merchant_canonical: "blue tokai" })]),
      ),
    );

    renderWithProviders(<RulesManager />);

    expect(await screen.findByText("blue tokai")).toBeInTheDocument();
  });
});
