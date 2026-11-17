import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { CardBillLinkField } from "./card-bill-link-field";
import { renderWithProviders, screen, waitFor } from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";
import { buildAccount, buildTransaction } from "@/tests/helpers/factories";

const accounts = [
  buildAccount({ id: 1, name: "Salary Account", type: "bank" }),
  buildAccount({ id: 2, name: "Amazon Pay CC", type: "credit_card" }),
];
const txn = buildTransaction({
  id: 30,
  account_id: 2,
  transaction_type: "income",
  amount_paise: 1500000,
});

// Server order: nearest date first, deliberately not date order.
const candidates = [
  buildTransaction({ id: 7, account_id: 1, merchant_raw: "PAY-ALPHA", date: "2026-08-14", amount_paise: -1500000 }),
  buildTransaction({ id: 8, account_id: 1, merchant_raw: "PAY-BRAVO", date: "2026-08-17", amount_paise: -1500000 }),
  buildTransaction({ id: 9, account_id: 1, merchant_raw: "PAY-CHARLIE", date: "2026-08-10", amount_paise: -1500000 }),
];

function renderField(onLinked = vi.fn()) {
  const utils = renderWithProviders(
    <CardBillLinkField txn={txn} accounts={accounts} onLinked={onLinked} />,
  );
  return { ...utils, onLinked };
}

describe("CardBillLinkField (ADR-0002 rule 11b)", () => {
  it("lists the server's link candidates in the order given", async () => {
    server.use(
      http.get("*/api/v1/transactions/30/link-candidates", () => HttpResponse.json(candidates)),
    );
    const { user } = renderField();

    await user.click(screen.getByRole("button", { name: /Pick the payment/ }));
    const items = await screen.findAllByRole("menuitem");
    expect(items.map((i) => /PAY-\w+/.exec(i.textContent ?? "")?.[0])).toEqual([
      "PAY-ALPHA",
      "PAY-BRAVO",
      "PAY-CHARLIE",
    ]);
  });

  it("links the picked payment, refreshes transactions and dashboards, then calls onLinked", async () => {
    let body: unknown = null;
    server.use(
      http.get("*/api/v1/transactions/30/link-candidates", () => HttpResponse.json(candidates)),
      http.post("*/api/v1/transactions/30/link", async ({ request }) => {
        body = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { user, queryClient, onLinked } = renderField();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    await user.click(screen.getByRole("button", { name: /Pick the payment/ }));
    await user.click(await screen.findByText(/PAY-ALPHA/));

    await waitFor(() => expect(onLinked).toHaveBeenCalled());
    expect(body).toEqual({ partner_id: 7 });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["transactions"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["dashboards"] });
  });

  it("shows the server's reason when the link is refused", async () => {
    server.use(
      http.get("*/api/v1/transactions/30/link-candidates", () => HttpResponse.json(candidates)),
      http.post("*/api/v1/transactions/30/link", () =>
        HttpResponse.json(
          { detail: "the two amounts must be exactly equal and opposite" },
          { status: 422 },
        ),
      ),
    );
    const { user, onLinked } = renderField();

    await user.click(screen.getByRole("button", { name: /Pick the payment/ }));
    await user.click(await screen.findByText(/PAY-BRAVO/));

    expect(
      await screen.findByText("the two amounts must be exactly equal and opposite"),
    ).toBeInTheDocument();
    expect(onLinked).not.toHaveBeenCalled();
  });

  it("says so when no payment matches", async () => {
    const { user } = renderField();

    await user.click(screen.getByRole("button", { name: /Pick the payment/ }));
    expect(await screen.findByText("No matching payment found")).toBeInTheDocument();
  });
});
