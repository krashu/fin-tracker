import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { AddInstrumentDialog } from "./add-instrument";
import {
  renderWithProviders,
  screen,
  waitFor,
} from "@/tests/helpers/test-utils";
import { mockAssetClassCurrencies } from "@/tests/mocks/handlers";
import { server } from "@/tests/mocks/server";

function captureCreate() {
  const captured: { body: Record<string, unknown> | null } = { body: null };
  server.use(
    http.post("*/api/v1/instruments", async ({ request }) => {
      captured.body = (await request.json()) as Record<string, unknown>;
      return HttpResponse.json({ id: 1, ...captured.body }, { status: 201 });
    }),
  );
  return captured;
}

async function fillAndPick(
  user: ReturnType<typeof renderWithProviders>["user"],
  className: string,
) {
  await user.type(screen.getByLabelText("Symbol"), "SYM");
  await user.type(screen.getByLabelText("Name"), "Some instrument");
  await user.click(screen.getByRole("button", { name: /Asset class/ }));
  await user.click(await screen.findByRole("menuitem", { name: className }));
}

describe("AddInstrumentDialog currency (B19)", () => {
  it("posts the currency the server fixes for the class", async () => {
    server.use(
      http.get("*/api/v1/instruments/asset-classes", () =>
        HttpResponse.json(
          mockAssetClassCurrencies.map((r) =>
            r.asset_class === "other" ? { ...r, currencies: ["USD"] } : r,
          ),
        ),
      ),
    );
    const captured = captureCreate();
    const { user } = renderWithProviders(
      <AddInstrumentDialog onClose={vi.fn()} />,
    );

    await fillAndPick(user, "Other");
    await user.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => expect(captured.body?.currency).toBe("USD"));
  });

  it("offers a currency choice for a class the server leaves free", async () => {
    const captured = captureCreate();
    const { user } = renderWithProviders(
      <AddInstrumentDialog onClose={vi.fn()} />,
    );

    await fillAndPick(user, "Fixed deposit");
    expect(
      await screen.findByRole("button", { name: /Currency/ }),
    ).not.toBeNull();
    await user.click(screen.getByRole("button", { name: /Currency/ }));
    await user.click(await screen.findByRole("menuitem", { name: "USD" }));
    await user.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => expect(captured.body?.currency).toBe("USD"));
  });

  it("says when the currencies cannot load", async () => {
    server.use(
      http.get("*/api/v1/instruments/asset-classes", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    const { user } = renderWithProviders(
      <AddInstrumentDialog onClose={vi.fn()} />,
    );

    await user.type(screen.getByLabelText("Symbol"), "SYM");
    await user.type(screen.getByLabelText("Name"), "Some instrument");

    expect(
      await screen.findByText("Couldn’t load currencies."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
  });

  it("defaults exchange to NASDAQ when US equity is selected", async () => {
    const captured = captureCreate();
    const { user } = renderWithProviders(
      <AddInstrumentDialog onClose={vi.fn()} />,
    );

    await fillAndPick(user, "US equity");
    await user.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => {
      expect(captured.body?.asset_class).toBe("us_equity");
      expect(captured.body?.exchange).toBe("NASDAQ");
    });
  });
});
