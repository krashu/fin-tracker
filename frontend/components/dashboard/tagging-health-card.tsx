"use client";

/**
 * Auto-tag acceptance metric (PRD §F3 / §Success-metrics: ≥80% of imports
 * pre-tagged correctly). Of board rows the import auto-tagged, the share whose
 * final category still equals the suggestion. Deliberately a modest card, not a
 * headline tile — the PRD treats this as a periodic health check (a quarterly
 * "is exact-match auto-tag still good enough, or pull fuzzy/rules forward?"),
 * not a daily number. `acceptance_rate === null` means no auto-tagged imports
 * yet → "—", never "0%". Shares the `["dashboards"]` invalidation prefix, so it
 * refreshes after an import commit or a category edit.
 */
import { useQuery } from "@tanstack/react-query";

import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { getTaggingStats } from "@/lib/api/client";
import { cn } from "@/lib/utils";

export function TaggingHealthCard() {
  const query = useQuery({
    queryKey: ["dashboards", "tagging-stats"],
    queryFn: getTaggingStats,
  });
  const stats = query.data;
  const pct =
    stats?.acceptance_rate == null
      ? null
      : Math.round(stats.acceptance_rate * 100);
  const rateColor =
    pct == null
      ? "text-muted-foreground"
      : pct >= 80
        ? "text-pos"
        : "text-[color:var(--warn)]";

  return (
    // A periodic health check, not spending analysis — deliberately de-emphasized
    // below the analytics: compact (size="sm") + a muted surface so it recedes
    // to a quiet footer widget rather than competing with the charts above.
    <Card size="sm" className="bg-muted/30">
      <CardHeader className="flex flex-row items-center justify-between border-b">
        <div className="flex flex-col gap-0.5">
          <span className="text-[13px] font-medium text-foreground">
            Auto-tag accuracy
          </span>
          <span className="text-[11.5px] text-muted-foreground">
            Imported rows kept with their suggested category · target ≥ 80%
          </span>
        </div>
        <span
          className={cn("text-[15px] font-semibold tabular-nums", rateColor)}
          style={{ fontVariantNumeric: "tabular-nums lining-nums" }}
        >
          {query.isPending ? " " : pct == null ? "—" : `${pct}%`}
        </span>
      </CardHeader>
      <CardContent className="py-3 text-[12px] text-muted-foreground">
        {query.isError
          ? "Couldn’t load — is the API running?"
          : stats == null
            ? " "
            : stats.total_auto_tagged === 0
              ? "No auto-tagged imports yet."
              : `${stats.kept} of ${stats.total_auto_tagged} auto-tagged rows kept · ` +
                `${stats.rules_count} learned ${stats.rules_count === 1 ? "rule" : "rules"}`}
      </CardContent>
    </Card>
  );
}
