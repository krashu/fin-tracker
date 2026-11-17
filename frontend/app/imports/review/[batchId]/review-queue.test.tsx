import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { ReviewQueue } from "./review-queue";
import type {
  BatchReconciliation,
  TransactionCandidate,
} from "@/lib/api/client";
import { TooltipProvider } from "@/components/ui/tooltip";
import { buildTransaction } from "@/tests/helpers/factories";
import {
  renderWithProviders,
  screen,
  waitFor,
} from "@/tests/helpers/test-utils";
import { server } from "@/tests/mocks/server";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

const BATCH_ID = 7;

function candidate(
  overrides: Partial<TransactionCandidate>,
): TransactionCandidate {
  return {
    ...buildTransaction({ category_id: null, category_name: null }),
    prior_matches: 0,
    confidence: "none",
    pinned: false,
    cc_payment_candidate: false,
    uncategorised_commits_as_transfer: false,
    ...overrides,
  };
}

const unavailable: BatchReconciliation = {
  batch_id: BATCH_ID,
  opening_balance_paise: null,
  closing_balance_paise: null,
  period_start: null,
  period_end: null,
  expected_paise: null,
  actual_paise: null,
  delta_paise: null,
  status: "unavailable",
  rows_removed_since_import: 0,
};

function serve(candidates: TransactionCandidate[]) {
  server.use(
    http.get(`*/api/v1/imports/${BATCH_ID}/candidates`, () =>
      HttpResponse.json(candidates),
    ),
    http.get(`*/api/v1/imports/${BATCH_ID}/reconciliation`, () =>
      HttpResponse.json(unavailable),
    ),
  );
}

describe("ReviewQueue card-bill payment option (PRD §F4a-1)", () => {
  it("offers 'Card bill payment' on a flagged bank debit, with the card-side copy", async () => {
    serve([
      candidate({
        id: 1,
        merchant_raw: "CC 000000XXXXXX0000 AUTOPAY SI-TAD",
        amount_paise: -123456,
        transaction_type: "spend",
        cc_payment_candidate: true,
        uncategorised_commits_as_transfer: true,
      }),
    ]);

    const { user } = renderWithProviders(
      <TooltipProvider>
        <ReviewQueue batchId={BATCH_ID} />
      </TooltipProvider>,
    );

    // An untagged spend is not default-staged, so it starts as an unpicked category.
    await user.click(
      await screen.findByRole("button", { name: /Pick category/ }),
    );

    expect(await screen.findByText("Card bill payment")).toBeInTheDocument();
    expect(
      screen.getByText(/links to your card's payment at commit/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/otherwise it stays out of spend as an unlinked transfer/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/imports as Other/)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/links to your bank debit at commit/),
    ).not.toBeInTheDocument();

    await user.click(screen.getByText("Card bill payment"));
    expect(
      await screen.findByRole("button", { name: /Card payment/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByTitle(/stays out of spend if none matches/),
    ).toBeInTheDocument();
    // The server says it commits as a transfer, so the header does not count it as Other.
    expect(screen.queryByText(/will import as/)).not.toBeInTheDocument();
  });

  it("says a flagged bank debit imports as Other when no covering card statement is in", async () => {
    serve([
      candidate({
        id: 1,
        merchant_raw: "CC 000000XXXXXX0000 AUTOPAY SI-TAD",
        amount_paise: -123456,
        transaction_type: "spend",
        cc_payment_candidate: true,
        uncategorised_commits_as_transfer: false,
      }),
    ]);

    const { user } = renderWithProviders(
      <TooltipProvider>
        <ReviewQueue batchId={BATCH_ID} />
      </TooltipProvider>,
    );

    await user.click(
      await screen.findByRole("button", { name: /Pick category/ }),
    );
    expect(
      await screen.findByText(/otherwise it imports as Other until it is linked/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/stays out of spend/)).not.toBeInTheDocument();

    await user.click(screen.getByText("Card bill payment"));
    expect(
      await screen.findByTitle(/imports as Other if none matches/),
    ).toBeInTheDocument();
    expect(await screen.findByText(/1 will import as/)).toBeInTheDocument();
  });

  it("does not offer it on an unflagged debit", async () => {
    serve([
      candidate({
        id: 2,
        merchant_raw: "UPI-SENTINEL GROCER",
        amount_paise: -50000,
        transaction_type: "spend",
      }),
    ]);

    const { user } = renderWithProviders(
      <TooltipProvider>
        <ReviewQueue batchId={BATCH_ID} />
      </TooltipProvider>,
    );

    await user.click(
      await screen.findByRole("button", { name: /Pick category/ }),
    );

    expect(await screen.findByText("Uncategorized")).toBeInTheDocument();
    expect(screen.queryByText("Card bill payment")).not.toBeInTheDocument();
  });

  it("says a flagged card credit stays out of income when no bank debit matches", async () => {
    // ADR-0002 rule 7c: the server says an uncategorised flagged credit commits as an
    // unpaired transfer, so the copy never promises income.
    serve([
      candidate({
        id: 3,
        merchant_raw: "PAYMENT RECEIVED THANK YOU",
        amount_paise: 30000,
        transaction_type: "income",
        cc_payment_candidate: true,
        uncategorised_commits_as_transfer: true,
      }),
    ]);

    const { user } = renderWithProviders(
      <TooltipProvider>
        <ReviewQueue batchId={BATCH_ID} />
      </TooltipProvider>,
    );

    // A credit is not default-staged, so it starts as an unpicked category.
    await user.click(
      await screen.findByRole("button", { name: /Pick category/ }),
    );
    expect(
      await screen.findByText(
        /otherwise it stays out of income as an unlinked transfer/,
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/links to your card's payment at commit/),
    ).not.toBeInTheDocument();

    await user.click(screen.getByText("Card bill payment"));
    expect(
      await screen.findByRole("button", { name: /Card payment/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByTitle(/stays out of income if none matches/),
    ).toBeInTheDocument();
  });
});

describe("ReviewQueue commit and discard in one request (ADR-0002 rule 5)", () => {
  function twoRows() {
    serve([
      candidate({
        id: 11,
        merchant_raw: "NETFLIX",
        amount_paise: -64900,
        transaction_type: "spend",
        category_id: 1,
        category_name: "Food",
      }),
      candidate({
        id: 12,
        merchant_raw: "ZOMATO",
        amount_paise: -35000,
        transaction_type: "spend",
      }),
    ]);
  }

  it("commits and discards in one request", async () => {
    twoRows();
    let body: unknown = null;
    const deleteSpy = vi.fn();
    server.use(
      http.post(`*/api/v1/imports/${BATCH_ID}/commit`, async ({ request }) => {
        body = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
      http.delete("*/api/v1/transactions/:id", () => {
        deleteSpy();
        return new HttpResponse(null, { status: 204 });
      }),
    );

    const { user } = renderWithProviders(
      <TooltipProvider>
        <ReviewQueue batchId={BATCH_ID} />
      </TooltipProvider>,
    );

    await user.click(await screen.findByRole("button", { name: "Discard ZOMATO" }));
    await user.click(
      await screen.findByRole("button", { name: /Commit 1 · remove 1/ }),
    );

    await waitFor(() =>
      expect(body).toEqual({ transaction_ids: [11], discard_ids: [12] }),
    );
    expect(deleteSpy).not.toHaveBeenCalled();
  });

  it("a 422 naming a discarded row unmarks it", async () => {
    twoRows();
    server.use(
      http.post(`*/api/v1/imports/${BATCH_ID}/commit`, () =>
        HttpResponse.json(
          {
            detail: {
              message: "some transactions are not eligible to commit or discard",
              invalid_ids: [12],
            },
          },
          { status: 422 },
        ),
      ),
    );

    const { user } = renderWithProviders(
      <TooltipProvider>
        <ReviewQueue batchId={BATCH_ID} />
      </TooltipProvider>,
    );

    await user.click(await screen.findByRole("button", { name: "Discard ZOMATO" }));
    expect(
      await screen.findByRole("button", { name: "Restore ZOMATO" }),
    ).toBeInTheDocument();
    await user.click(
      await screen.findByRole("button", { name: /Commit 1 · remove 1/ }),
    );

    expect(
      await screen.findByRole("button", { name: "Discard ZOMATO" }),
    ).toBeInTheDocument();
  });
});
