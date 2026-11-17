import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { currentFy } from "@/lib/period";
import { server } from "@/tests/mocks/server";
import { renderWithProviders, screen, waitFor } from "@/tests/helpers/test-utils";
import { TaxStatement } from "./tax-statement";

describe("TaxStatement (app/tax/tax-statement.tsx)", () => {
  it("asks for the picked tax year", async () => {
    const asked: (string | null)[] = [];
    const now = currentFy();
    server.use(
      http.get("*/api/v1/tax/available-years", () =>
        HttpResponse.json({ years: [now, 2025] }),
      ),
      http.get("*/api/v1/tax/intraday-trades", ({ request }) => {
        asked.push(new URL(request.url).searchParams.get("year"));
        return HttpResponse.json({ period: "x", trades: [], total_profit_paise: 0 });
      }),
    );
    const { user } = renderWithProviders(<TaxStatement />);

    await waitFor(() => expect(asked).toContain(String(now)));
    await user.click(screen.getByRole("button", { name: /^(Tax year|FY) / }));
    await user.click(await screen.findByRole("menuitem", { name: "FY 2025-26 (AY 2026-27)" }));

    await waitFor(() => expect(asked.at(-1)).toBe("2025"));
  });
});
