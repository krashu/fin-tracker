import { describe, expect, it } from "vitest";
import {
  createInvestmentAccount,
  deleteInvestmentAccount,
  getInvestmentAccount,
  getInvestmentAccounts,
  updateInvestmentAccount,
} from "./investment-accounts";
import { mockInvestmentAccounts } from "@/tests/mocks/handlers";

describe("investment-accounts API client", () => {
  it("fetches active investment accounts list", async () => {
    const accounts = await getInvestmentAccounts({ is_active: true });
    expect(accounts).toEqual(mockInvestmentAccounts);
  });

  it("fetches single investment account by id", async () => {
    const acc = await getInvestmentAccount(1);
    expect(acc).toEqual(mockInvestmentAccounts[0]);
  });

  it("creates a new investment account", async () => {
    const created = await createInvestmentAccount({
      name: "Groww MF Folio",
      account_type: "mf_folio",
      provider: "groww",
      external_account_ref: "groww_folio_1",
      currency: "INR",
    });
    expect(created.id).toBe(99);
    expect(created.name).toBe("Groww MF Folio");
  });

  it("updates an investment account", async () => {
    const updated = await updateInvestmentAccount(1, {
      name: "Zerodha Primary Demat",
    });
    expect(updated.id).toBe(1);
    expect(updated.name).toBe("Zerodha Primary Demat");
  });

  it("deletes an investment account", async () => {
    await expect(deleteInvestmentAccount(1)).resolves.toBeUndefined();
  });
});
