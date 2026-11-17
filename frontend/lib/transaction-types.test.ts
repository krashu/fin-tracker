import { describe, expect, it } from "vitest";
import {
  EDITABLE_TRANSACTION_TYPES,
  ENTRY_DIRECTIONS,
  ENTRY_DIRECTION_LABELS,
  TRANSACTION_TYPE_LABELS,
  directionForTransaction,
  directionToType,
  isRefund,
  signedPaise,
} from "@/lib/transaction-types";

describe("transaction-types ADR-0009 and F2 rules", () => {
  it("restricts directly editable types to spend and income", () => {
    expect(EDITABLE_TRANSACTION_TYPES).toEqual(["spend", "income"]);
    // Transfer cannot be minted as a single leg
    expect(EDITABLE_TRANSACTION_TYPES).not.toContain("transfer");
  });

  it("identifies refunds as spend transactions with positive amount_paise", () => {
    // Standard spend is negative
    expect(isRefund({ transaction_type: "spend", amount_paise: -50000 })).toBe(
      false,
    );
    // Refund is spend with positive amount
    expect(isRefund({ transaction_type: "spend", amount_paise: 50000 })).toBe(
      true,
    );
    // Income is not a refund even if positive
    expect(isRefund({ transaction_type: "income", amount_paise: 50000 })).toBe(
      false,
    );
    // Transfer is not a refund
    expect(isRefund({ transaction_type: "transfer", amount_paise: 50000 })).toBe(
      false,
    );
  });

  it("maps 4-way UI entry directions to underlying stored types", () => {
    expect(directionToType("spend")).toBe("spend");
    expect(directionToType("refund")).toBe("spend");
    expect(directionToType("income")).toBe("income");
    expect(directionToType("income_reversal")).toBe("income");
  });

  it("maps the income-reversal direction to (income, −) and recovers it from a negative income row", () => {
    expect(ENTRY_DIRECTIONS).toEqual([
      "spend",
      "refund",
      "income",
      "income_reversal",
    ]);
    expect(signedPaise("income_reversal", 5000)).toBe(-5000);
    expect(
      directionForTransaction({
        transaction_type: "income",
        amount_paise: -5000,
      }),
    ).toBe("income_reversal");
  });

  it("recovers UI entry direction from stored transaction row", () => {
    expect(
      directionForTransaction({
        transaction_type: "spend",
        amount_paise: -10000,
      }),
    ).toBe("spend");

    expect(
      directionForTransaction({
        transaction_type: "spend",
        amount_paise: 10000,
      }),
    ).toBe("refund");

    expect(
      directionForTransaction({
        transaction_type: "income",
        amount_paise: 100000,
      }),
    ).toBe("income");
  });

  it("labels the refund direction 'Refund / cashback' and maps it to (spend, +) (ADR-0013 rule 1)", () => {
    expect(ENTRY_DIRECTION_LABELS.refund).toBe("Refund / cashback");
    expect(directionToType("refund")).toBe("spend");
    expect(signedPaise("refund", 1000)).toBe(1000);
  });

  it("signs paise correctly based on entry direction", () => {
    const magnitude = 150000;
    // Outflow: negative
    expect(signedPaise("spend", magnitude)).toBe(-150000);
    // Refund / Inflow: positive
    expect(signedPaise("refund", magnitude)).toBe(150000);
    expect(signedPaise("income", magnitude)).toBe(150000);
    // Income reversal: negative income (ADR-0009 rule 12)
    expect(signedPaise("income_reversal", magnitude)).toBe(-150000);
  });

  it("has complete UI labels for all directions and transaction types", () => {
    expect(TRANSACTION_TYPE_LABELS.spend).toBe("Spend");
    expect(TRANSACTION_TYPE_LABELS.income).toBe("Income");
    expect(TRANSACTION_TYPE_LABELS.transfer).toBe("Transfer");

    expect(ENTRY_DIRECTION_LABELS.spend).toBe("Spend");
    expect(ENTRY_DIRECTION_LABELS.refund).toBe("Refund / cashback");
    expect(ENTRY_DIRECTION_LABELS.income).toBe("Income");
    expect(ENTRY_DIRECTION_LABELS.income_reversal).toBe("Income reversal");
  });
});
