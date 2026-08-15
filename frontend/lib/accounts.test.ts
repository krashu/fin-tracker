import { describe, expect, it } from "vitest";
import type { AccountRead } from "@/lib/api/client";
import { accountLabel, pendingBatchLabel } from "@/lib/accounts";

describe("accounts helpers", () => {
  it("formats account label with last4 if available", () => {
    const accWithLast4: AccountRead = {
      id: 1,
      name: "HDFC Regalia",
      type: "credit_card",
      currency: "INR",
      issuer: "HDFC",
      last4: "4321",
      opening_balance_paise: 0,
      parent_account_id: null,
      archived_at: null,
    };
    expect(accountLabel(accWithLast4)).toBe("HDFC Regalia ··4321");

    const accWithoutLast4: AccountRead = {
      ...accWithLast4,
      last4: null,
    };
    expect(accountLabel(accWithoutLast4)).toBe("HDFC Regalia");
  });

  it("formats pending batch labels with fallbacks", () => {
    expect(pendingBatchLabel("ICICI Bank", "9876")).toBe("ICICI Bank ··9876");
    expect(pendingBatchLabel("ICICI Bank", null)).toBe("ICICI Bank");
    expect(pendingBatchLabel(null, null)).toBe("Import");
    expect(pendingBatchLabel(null, "1234")).toBe("Import");
  });
});
