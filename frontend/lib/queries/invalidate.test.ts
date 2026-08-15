import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import { invalidateRules } from "./invalidate";

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
    qc.setQueryData(["rules"], [{ merchant_normalized: "swiggy" }]);
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
});
