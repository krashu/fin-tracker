"use client";

/**
 * Sync-icon button that triggers the price-refresh backfills from the UI — the
 * control the Portfolio performance card's "refresh NAVs / refresh benchmark
 * NAVs" hints had no home for (PRD §F7 / §F9).
 *
 * `scope` picks what to refresh by what the host page shows: the Portfolio page
 * shows both the summary and the benchmark comparison → `"all"` (holdings NAVs +
 * benchmark cache); the Holdings page shows positions only → `"navs"` (holdings
 * NAVs alone, no wasted benchmark fetch).
 *
 * The two endpoints are independent server-side backfills, fired in parallel via
 * `Promise.allSettled` — a benchmark-source hiccup must not mask a successful NAV
 * refresh. Crucially, both return HTTP 200 even when a *source* (AMFI / mfapi /
 * Yahoo) was unreachable, carrying the count + a `warnings` list with the real
 * cause. So we DON'T discard the result: three outcomes drive the UI —
 *  • clean   → no alert chrome; refetched data is the signal.
 *  • partial → some source failed (rejected / fetch_errors / warnings): the sync
 *              icon tints amber and a sibling alert button opens a details panel
 *              listing each source's real cause.
 *  • total   → every call rejected (API down): icon tints red, panel says so.
 * No toast (the app has none); an always-mounted aria-live region announces the
 * outcome to screen readers (the icon tint alone is invisible to them).
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";

import {
  type BenchmarkRefreshSummary,
  type NavRefreshSummary,
  refreshBenchmarks,
  refreshInstrumentNavs,
} from "@/lib/api/client";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { IconAlert, IconRefresh, IconX } from "@/components/icons";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

// `null` = the task ran but rejected (transport failure); a summary = it returned
// (possibly with its own per-source fetch_errors/warnings). `benchmark` is absent
// entirely for scope "navs" (it never ran).
type RefreshOutcome = {
  nav: NavRefreshSummary | null;
  benchmark?: BenchmarkRefreshSummary | null;
};

export function RefreshPricesButton({
  scope = "all",
}: {
  scope?: "all" | "navs";
}) {
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: async (): Promise<RefreshOutcome> => {
      if (scope === "navs") {
        const [navRes] = await Promise.allSettled([refreshInstrumentNavs()]);
        if (navRes.status === "rejected") throw navRes.reason;
        return { nav: navRes.value };
      }
      // Order is load-bearing: nav is the headline source, so on a total failure
      // we throw ITS reason; a benchmark-only failure stays a partial.
      const [navRes, benchRes] = await Promise.allSettled([
        refreshInstrumentNavs(),
        refreshBenchmarks(),
      ]);
      if (navRes.status === "rejected" && benchRes.status === "rejected") {
        throw navRes.reason;
      }
      return {
        nav: navRes.status === "fulfilled" ? navRes.value : null,
        benchmark: benchRes.status === "fulfilled" ? benchRes.value : null,
      };
    },
    onSuccess: () => {
      // Fires when at least one task succeeded. Mirror the investment-txn nudge:
      // holdings table, dashboard net worth, and the /portfolio tiles + per-holding
      // XIRR + benchmark comparison (the ["portfolio"] prefix covers summary AND
      // performance). A failed source self-shows via the panel; what DID update refetches.
      queryClient.invalidateQueries({ queryKey: ["holdings"] });
      queryClient.invalidateQueries({ queryKey: ["dashboards"] });
      queryClient.invalidateQueries({ queryKey: ["portfolio"] });
    },
  });

  // Read `data` only off the non-error path (when isError, data is undefined).
  const data = mutation.isError ? undefined : mutation.data;
  const navSummary = data?.nav ?? null;
  const benchSummary = data?.benchmark ?? null;
  const navRejected = !!data && data.nav === null;
  const benchRejected = !!data && data.benchmark === null;

  const warningsCount =
    (navSummary?.warnings.length ?? 0) + (benchSummary?.warnings.length ?? 0);
  const fetchErrors =
    (navSummary?.fetch_errors ?? 0) + (benchSummary?.fetch_errors ?? 0);
  // Only SOURCE-level failures count as a problem. `catalogue_staleness_days` deliberately
  // does NOT gate this, contrary to the remediation plan: it folds over every active
  // instrument, exited positions and hand-priced classes included, so one 200-day-old sold
  // gold row would tint the icon amber permanently, on both pages, with nothing the user
  // could do to clear it — and would make the aria-live region below announce "Some prices
  // couldn't be refreshed" after a refresh that succeeded completely. It is a diagnostic;
  // it belongs in the count line.
  const hasProblem =
    !!data &&
    (warningsCount > 0 || fetchErrors > 0 || navRejected || benchRejected);
  const isTotalFailure = mutation.isError;
  const showAlert = isTotalFailure || hasProblem;

  const iconTint = isTotalFailure
    ? "text-neg"
    : hasProblem
      ? "text-amber-600 dark:text-amber-500"
      : "";

  const liveMessage = mutation.isPending
    ? ""
    : isTotalFailure
      ? "Couldn’t refresh prices — the API is unreachable."
      : hasProblem
        ? "Some prices couldn’t be refreshed."
        : data
          ? "Prices updated."
          : "";

  // The count line reports what the run actually did. `stale_skipped` (a newer local value
  // was kept) and `catalogue_staleness_days` were computed, serialized and read by nobody
  // — a 45-day-old NAV counted as `mf_updated` and the panel said "0 errors".
  const navCount = navSummary
    ? [
        `${navSummary.mf_updated + navSummary.equity_updated} updated`,
        navSummary.stale_skipped > 0
          ? `${navSummary.stale_skipped} already current`
          : null,
        `${navSummary.fetch_errors} ${navSummary.fetch_errors === 1 ? "error" : "errors"}`,
        navSummary.catalogue_staleness_days != null
          ? `oldest ${navSummary.catalogue_staleness_days}d`
          : null,
      ]
        .filter(Boolean)
        .join(" · ")
    : null;
  // Not a problem signal, so it never reaches `hasProblem` — see the note there. It is
  // the answer to "why didn't pressing this fix my FD".
  const handPricedNote =
    navSummary && navSummary.skipped > 0
      ? `${navSummary.skipped} holding${navSummary.skipped === 1 ? " is" : "s are"} priced by hand — refreshing won’t update ${navSummary.skipped === 1 ? "it" : "them"}.`
      : null;
  const benchCount = benchSummary
    ? `${benchSummary.benchmarks_refreshed} refreshed · ${benchSummary.fetch_errors} ${benchSummary.fetch_errors === 1 ? "error" : "errors"}`
    : null;

  return (
    <div className="flex items-center gap-1">
      <Button
        type="button"
        variant="outline"
        size="icon-sm"
        onClick={() => mutation.mutate()}
        disabled={mutation.isPending}
        aria-label="Refresh prices"
        title="Refresh prices"
      >
        <IconRefresh
          className={cn(
            "size-4",
            mutation.isPending && "animate-spin",
            iconTint,
          )}
        />
      </Button>

      {showAlert ? (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              variant="outline"
              size="icon-sm"
              aria-label={
                isTotalFailure
                  ? "Refresh failed — view details"
                  : "Some prices couldn’t be refreshed — view details"
              }
              className={iconTint}
            >
              {isTotalFailure ? (
                <IconX className="size-4" />
              ) : (
                <IconAlert className="size-4" />
              )}
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent
            align="end"
            className="w-80 max-w-[calc(100vw-2rem)] p-3 text-[12.5px]"
          >
            {isTotalFailure ? (
              <p className="text-neg">
                Couldn’t reach the API — is the backend running?
              </p>
            ) : (
              <div className="flex flex-col gap-3">
                <RefreshSection
                  label={scope === "all" ? "Holdings NAVs" : null}
                  rejected={navRejected}
                  countLine={navCount}
                  note={handPricedNote}
                  warnings={navSummary?.warnings ?? []}
                />
                {scope === "all" ? (
                  <RefreshSection
                    label="Benchmark NAVs"
                    rejected={benchRejected}
                    countLine={benchCount}
                    warnings={benchSummary?.warnings ?? []}
                  />
                ) : null}
              </div>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      ) : null}

      <span className="sr-only" role="status" aria-live="polite">
        {liveMessage}
      </span>
    </div>
  );
}

// One source's outcome inside the details panel. A rejected task shows the
// unreachable note; otherwise the count line + each warning (which now carries the
// real cause from the backend). Rendered only when the parent has a problem to show.
function RefreshSection({
  label,
  rejected,
  countLine,
  note,
  warnings,
}: {
  label: string | null;
  rejected: boolean;
  countLine: string | null;
  /** A standing fact about the run, not a failure — e.g. "N holdings are priced by hand".
   * Shown alongside the warnings; on its own it is NOT enough to open this panel, since
   * having an FD is not a problem to be told about on every refresh. */
  note?: string | null;
  warnings: string[];
}) {
  // Skip a clean source entirely (no label/noise) so the panel stays problem-focused.
  if (!rejected && warnings.length === 0) return null;
  return (
    <div className="flex flex-col gap-1">
      {label ? <p className="font-medium text-foreground">{label}</p> : null}
      {rejected ? (
        <p className="text-neg">Couldn’t reach the API.</p>
      ) : (
        <>
          {countLine ? (
            <p className="text-muted-foreground">{countLine}</p>
          ) : null}
          {note ? <p className="text-muted-foreground">{note}</p> : null}
          {warnings.length > 0 ? (
            <ul className="ml-3 list-disc text-muted-foreground">
              {warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          ) : null}
        </>
      )}
    </div>
  );
}
