import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import {
  invalidateFriends,
  invalidateInvestmentAccounts,
  invalidateRules,
} from "./invalidate";

describe("TanStack Query Invalidation Contract (lib/queries/invalidate.ts)", () => {
  it("invalidates rules, tagging-stats, and all candidates cache keys", () => {
    const qc = new QueryClient({
      defaultOptions: {
        queries: {
          staleTime: 60_000,
        },
      },
    });

    // Seed queries in cache with data
    qc.setQueryData(["rules"], [{ merchant_canonical: "swiggy" }]);
    qc.setQueryData(["rules", "merchants"], ["swiggy", "zomato"]);
    qc.setQueryData(["rules", "aliases"], [{ pattern: "swiggy in", canonical: "swiggy" }]);
    qc.setQueryData(["dashboards", "tagging-stats"], { categorized_pct: 90 });
    qc.setQueryData(["dashboards", "summary"], { net_worth_paise: 100000 });
    qc.setQueryData(["candidates"], [{ id: 1 }]);
    qc.setQueryData(["candidates", 42], [{ id: 2 }]);
    qc.setQueryData(["candidates", "batch-100"], [{ id: 3 }]);
    qc.setQueryData(["accounts"], [{ id: 1, name: "Salary Account" }]);
    qc.setQueryData(["categories"], [{ id: 1, name: "Dining" }]);

    const cache = qc.getQueryCache();

    const getQuery = (queryKey: unknown[]) => cache.find({ queryKey });

    // Confirm all seeded queries are fresh (isStale() === false)
    expect(getQuery(["rules"])?.isStale()).toBe(false);
    expect(getQuery(["rules", "merchants"])?.isStale()).toBe(false);
    expect(getQuery(["rules", "aliases"])?.isStale()).toBe(false);
    expect(getQuery(["dashboards", "tagging-stats"])?.isStale()).toBe(false);
    expect(getQuery(["dashboards", "summary"])?.isStale()).toBe(false);
    expect(getQuery(["candidates"])?.isStale()).toBe(false);
    expect(getQuery(["candidates", 42])?.isStale()).toBe(false);
    expect(getQuery(["candidates", "batch-100"])?.isStale()).toBe(false);
    expect(getQuery(["accounts"])?.isStale()).toBe(false);
    expect(getQuery(["categories"])?.isStale()).toBe(false);

    // Call invalidation contract
    invalidateRules(qc);

    // Verified: Rules queries (exact & prefixed) are invalidated (stale)
    expect(getQuery(["rules"])?.isStale()).toBe(true);
    expect(getQuery(["rules", "merchants"])?.isStale()).toBe(true);
    expect(getQuery(["rules", "aliases"])?.isStale()).toBe(true);

    // Verified: Tagging-stats is invalidated, but sibling dashboard summary is NOT
    expect(getQuery(["dashboards", "tagging-stats"])?.isStale()).toBe(true);
    expect(getQuery(["dashboards", "summary"])?.isStale()).toBe(false);

    // Verified: All candidate batches (prefix matching ["candidates"]) are invalidated
    expect(getQuery(["candidates"])?.isStale()).toBe(true);
    expect(getQuery(["candidates", 42])?.isStale()).toBe(true);
    expect(getQuery(["candidates", "batch-100"])?.isStale()).toBe(true);

    // Verified: Completely unrelated queries remain untouched
    expect(getQuery(["accounts"])?.isStale()).toBe(false);
    expect(getQuery(["categories"])?.isStale()).toBe(false);
  });

  it("invalidates friends and dashboards queries via invalidateFriends", () => {
    const qc = new QueryClient({
      defaultOptions: {
        queries: {
          staleTime: 60_000,
        },
      },
    });

    qc.setQueryData(["friends", "summary"], { net_balance_paise: 50000 });
    qc.setQueryData(["friends", "contacts"], [{ id: 1, name: "Rahul" }]);
    qc.setQueryData(["friends", "contacts", 1, "entries"], [{ id: 10 }]);
    qc.setQueryData(["dashboards", "summary"], { net_worth_paise: 100000 });
    qc.setQueryData(["accounts"], [{ id: 1 }]);

    const cache = qc.getQueryCache();
    const getQuery = (queryKey: unknown[]) => cache.find({ queryKey });

    expect(getQuery(["friends", "summary"])?.isStale()).toBe(false);
    expect(getQuery(["friends", "contacts"])?.isStale()).toBe(false);
    expect(getQuery(["friends", "contacts", 1, "entries"])?.isStale()).toBe(false);
    expect(getQuery(["dashboards", "summary"])?.isStale()).toBe(false);
    expect(getQuery(["accounts"])?.isStale()).toBe(false);

    invalidateFriends(qc);

    expect(getQuery(["friends", "summary"])?.isStale()).toBe(true);
    expect(getQuery(["friends", "contacts"])?.isStale()).toBe(true);
    expect(getQuery(["friends", "contacts", 1, "entries"])?.isStale()).toBe(true);
    expect(getQuery(["dashboards", "summary"])?.isStale()).toBe(true);
    expect(getQuery(["accounts"])?.isStale()).toBe(false);
  });

  it("invalidates investment-accounts and holdings cache keys", () => {
    const qc = new QueryClient({
      defaultOptions: {
        queries: {
          staleTime: 60_000,
        },
      },
    });

    qc.setQueryData(["investment-accounts"], [{ id: 1, name: "Zerodha" }]);
    qc.setQueryData(["investment-accounts", { is_active: true }], [{ id: 1 }]);
    qc.setQueryData(["holdings"], { holdings: [] });
    qc.setQueryData(["holdings", { account_id: 1 }], { holdings: [] });
    qc.setQueryData(["accounts"], [{ id: 1 }]);

    const cache = qc.getQueryCache();
    const getQuery = (queryKey: unknown[]) => cache.find({ queryKey });

    expect(getQuery(["investment-accounts"])?.isStale()).toBe(false);
    expect(getQuery(["investment-accounts", { is_active: true }])?.isStale()).toBe(false);
    expect(getQuery(["holdings"])?.isStale()).toBe(false);
    expect(getQuery(["holdings", { account_id: 1 }])?.isStale()).toBe(false);
    expect(getQuery(["accounts"])?.isStale()).toBe(false);

    invalidateInvestmentAccounts(qc);

    expect(getQuery(["investment-accounts"])?.isStale()).toBe(true);
    expect(getQuery(["investment-accounts", { is_active: true }])?.isStale()).toBe(true);
    expect(getQuery(["holdings"])?.isStale()).toBe(true);
    expect(getQuery(["holdings", { account_id: 1 }])?.isStale()).toBe(true);
    expect(getQuery(["accounts"])?.isStale()).toBe(false);
  });
});
