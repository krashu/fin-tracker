"use client";

/**
 * /spending dashboard island (PRD §F8 views 2 + 3, spending-only). Composes the
 * monthly spend-by-category breakdown and the spend-over-time bar. This shell owns
 * two pieces of shared state: the F3a tag cross-filter (PRD §F3a labels), threaded
 * into the spend charts as `labelId` so a picked tag scopes them all at once, and
 * the month anchor for the three MONTHLY cards (spend-by-category, spend-by-tag,
 * top-merchants) so their three steppers move as one. The trend cards keep their own
 * period state — a grain toggle is a different control from a month, and no finding
 * asks for them to follow along. Views 1 & 4 (live portfolio tiles, net worth over
 * time) land with F7 — this surface is the spending slice of F8.
 *
 * Card dispositions under the tag filter:
 *  - SpendByCategory / TopMerchants / SpendByPeriodBar / CategoryTrendBar → scoped.
 *  - CashflowBar → whole-account (income can't carry tags, so a tag filter would
 *    zero income and make "am I solvent" meaningless); it shows a caption instead.
 *  - TaggingHealthCard → auto-tag *health* metric, not a spend view → unscoped.
 *  - SpendByTag (spend-by-tag breakdown + coverage, arc Phase B) → the tag
 *    breakdown itself → unscoped (filtering it to one tag would collapse it to a
 *    single bar). Hidden until the user has ≥ 1 tag, like the filter chip, so a
 *    tag-less catalog leaves the page exactly as it was.
 */
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { CashflowBar } from "@/components/dashboard/cashflow-bar";
import { CategoryTrendBar } from "@/components/dashboard/category-trend-bar";
import { PeriodPicker } from "@/components/dashboard/period-picker";
import { SpendByCategory } from "@/components/dashboard/spend-by-category";
import { SpendByPeriodBar } from "@/components/dashboard/spend-by-period-bar";
import { SpendByTag } from "@/components/dashboard/spend-by-tag";
import { SpendByTagHeatmap } from "@/components/dashboard/spend-by-tag-heatmap";
import { TaggingHealthCard } from "@/components/dashboard/tagging-health-card";
import { TopMerchants } from "@/components/dashboard/top-merchants";
import { useAvailableYears } from "@/components/dashboard/use-available-years";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Pill } from "@/components/ui/pill";
import { IconChevronDown } from "@/components/icons";
import { listLabels } from "@/lib/api/client";
import { labelDisplay } from "@/lib/labels";
import { periodKey, type Period } from "@/lib/period";

export function SpendingDashboard() {
  const yearsQuery = useAvailableYears();
  const [period, setPeriod] = useState<Period>(() => ({
    year: new Date().getFullYear(),
  }));
  const [labelId, setLabelId] = useState<number | undefined>(undefined);

  const labelsQuery = useQuery({ queryKey: ["labels"], queryFn: listLabels });
  const labels = labelsQuery.data ?? [];

  // Orphan-filter guard: if the selected tag is deleted, clear it.
  useEffect(() => {
    const data = labelsQuery.data;
    if (labelId != null && data && !data.some((l) => l.id === labelId)) {
      setLabelId(undefined);
    }
  }, [labelId, labelsQuery.data]);

  const selectedTag = labels.find((l) => l.id === labelId);
  const selectedYear = period.year;
  const monthParam = period.mon != null ? periodKey(period) : undefined;

  return (
    <div className="flex flex-col gap-5">
      {/* Top Filter Controls: Period selector + Tag selector */}
      <div className="flex items-center gap-4 flex-wrap">
        <PeriodPicker
          period={period}
          availableYears={yearsQuery.years}
          onChange={setPeriod}
        />

        {labels.length > 0 ? (
          <div className="flex items-center gap-2">
            <span className="text-[12.5px] text-muted-foreground">
              Filter by tag
            </span>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Pill active={labelId != null}>
                  {selectedTag ? labelDisplay(selectedTag.name) : "All tags"}
                  <IconChevronDown className="size-3 opacity-70" />
                </Pill>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="max-h-72 w-48">
                <DropdownMenuItem onSelect={() => setLabelId(undefined)}>
                  <span className="text-muted-foreground">All tags</span>
                </DropdownMenuItem>
                {labels.map((l) => (
                  <DropdownMenuItem key={l.id} onSelect={() => setLabelId(l.id)}>
                    {labelDisplay(l.name)}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        ) : null}
      </div>

      <SpendByCategory labelId={labelId} year={selectedYear} month={monthParam} />
      {labels.length > 0 ? <SpendByTag year={selectedYear} month={monthParam} /> : null}
      <TopMerchants labelId={labelId} year={selectedYear} month={monthParam} />
      <CashflowBar tagActive={labelId != null} year={selectedYear} />
      <SpendByPeriodBar labelId={labelId} year={selectedYear} />
      <CategoryTrendBar labelId={labelId} year={selectedYear} />
      {labels.length > 0 ? <SpendByTagHeatmap year={selectedYear} /> : null}
      <TaggingHealthCard />
    </div>
  );
}
