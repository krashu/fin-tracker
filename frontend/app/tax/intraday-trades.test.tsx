import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { buildIntradayTrade } from "@/tests/helpers/factories";
import { server } from "@/tests/mocks/server";
import { renderWithProviders, screen } from "@/tests/helpers/test-utils";
import { IntradayTrades } from "./intraday-trades";

function serve(trades: ReturnType<typeof buildIntradayTrade>[], total: number) {
  server.use(
    http.get("*/api/v1/tax/intraday-trades", () =>
      HttpResponse.json({ period: "2025", trades, total_profit_paise: total }),
    ),
  );
}

describe("IntradayTrades (app/tax/intraday-trades.tsx)", () => {
  it("lists each intraday match with the server's profit, apart from capital gains", async () => {
    // Profit is deliberately not sell - buy (that would be 500.00): the server's figure wins.
    serve([buildIntradayTrade()], 49500);
    renderWithProviders(<IntradayTrades year={2025} />, { initialBalanceHidden: false });

    expect(await screen.findByText("INFY")).toBeInTheDocument();
    const row = screen.getByText("INFY").closest("tr");
    expect(row).toHaveTextContent("₹495.00");
    expect(row).not.toHaveTextContent("₹500.00");
    expect(screen.getByText(/Speculative business income/)).toBeInTheDocument();
  });

  it("masks every amount while balances are hidden", async () => {
    serve([buildIntradayTrade()], 49500);
    renderWithProviders(<IntradayTrades year={2025} />, { initialBalanceHidden: true });

    expect(await screen.findByText("INFY")).toBeInTheDocument();
    expect(screen.queryByText(/₹/)).toBeNull();
  });

  it("shows the server's total, not a client-side sum of the rows", async () => {
    serve(
      [
        buildIntradayTrade({ symbol: "AAA", profit_paise: 40000 }),
        buildIntradayTrade({ symbol: "BBB", profit_paise: 9500 }),
      ],
      // Deliberately not 40000 + 9500: the figure must come from the server.
      12345,
    );
    renderWithProviders(<IntradayTrades year={2025} />, { initialBalanceHidden: false });

    const total = (await screen.findByText("Total")).closest("tr");
    expect(total).not.toBeNull();
    expect(total).toHaveTextContent("₹123.45");
    expect(total).not.toHaveTextContent("₹495.00");
  });

  it("says there were no intraday trades in an empty year", async () => {
    serve([], 0);
    renderWithProviders(<IntradayTrades year={2025} />, { initialBalanceHidden: false });

    expect(
      await screen.findByText("No intraday trades in FY 2025-26 (AY 2026-27)."),
    ).toBeInTheDocument();
  });
});
