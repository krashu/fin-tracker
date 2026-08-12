import type { QueryClient } from "@tanstack/react-query";

/**
 * Invalidate every cache that a merchant→category/label *learning* event can
 * change: the rules list (`/settings/rules`), the tagging-health tiles
 * (`/dashboard`), and any open import review queue's per-row confidence.
 *
 * Call this from every mutation that learns or mutates a rule — the import
 * commit (the primary learning event), board txn create/edit (which run
 * `learn_merchant_memory` server-side), the category/label managers (a
 * delete/rename changes which rules are live / their joined names), and the
 * ALIAS manager's create / rename-canonical / delete (ADR-0011: an alias decides
 * which raw descriptors fold into one canonical, so every one of the three
 * re-shapes the grouped rules list and the review queue's per-row confidence).
 * Without it, with `staleTime: 30_000` + `refetchOnWindowFocus: false`, those
 * surfaces serve a stale list and rule actions can 404 on a now-deleted id.
 *
 * `["candidates"]` is intentionally the bare prefix so it matches every
 * `["candidates", batchId]`.
 */
export function invalidateRules(qc: QueryClient): void {
  qc.invalidateQueries({ queryKey: ["rules"] });
  qc.invalidateQueries({ queryKey: ["dashboards", "tagging-stats"] });
  qc.invalidateQueries({ queryKey: ["candidates"] });
}
