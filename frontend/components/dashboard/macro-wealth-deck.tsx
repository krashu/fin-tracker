"use client";

/**
 * Tier 3: Macro Wealth Horizon Deck (PRD §F8 View 4 & Strategic Wealth Horizon).
 *
 * Consolidates low-frequency, strategic wealth visualizers into an elegant tabbed deck:
 * - Tab 1: 📈 Net Worth Trajectory (Total area or 7-asset breakdown, liabilities overlay, milestones, 1M-ALL)
 * - Tab 2: 🌊 Cashflow Waterfall (Horizontal annual conversion of cash inflows into living expenses, peer dues, investments, surplus)
 *
 * Implements a structured Two-Tier Header Layout:
 * - Top Tier: Deck Title & Radix Tab Selector + Portfolio Link
 * - Sub-Tier Bar: Contextual controls (Total/Breakdown + Range for Net Worth; CY/FY + Year Picker for Waterfall)
 */

import { useState } from "react";
import Link from "next/link";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { NetWorthCard } from "@/components/dashboard/net-worth-card";
import { CashflowWaterfallCard } from "@/components/dashboard/cashflow-waterfall-card";
import { useAvailableYears } from "@/components/dashboard/use-available-years";
import {
  type OverviewResponse,
  type TimelineRange,
  type YearType,
} from "@/lib/api/client";
import { currentYear } from "@/lib/period";
import { cn } from "@/lib/utils";

const RANGES: TimelineRange[] = ["1M", "6M", "1Y", "3Y", "5Y", "ALL"];

interface MacroWealthDeckProps {
  overview?: OverviewResponse;
}

export function MacroWealthDeck({ overview }: MacroWealthDeckProps) {
  const [activeTab, setActiveTab] = useState<string>("trajectory");
  const [composition, setComposition] = useState<"total" | "breakdown">("total");
  const [range, setRange] = useState<TimelineRange>("1Y");
  const [yearType, setYearType] = useState<YearType>("calendar");
  const [selectedYear, setSelectedYear] = useState<number | null>(null);

  const yearsQuery = useAvailableYears();
  const effectiveYear = selectedYear ?? yearsQuery.years[0] ?? currentYear();

  return (
    <section
      aria-label="Macro Wealth Horizon"
      className="overflow-hidden rounded-lg border border-border bg-card"
    >
      <Tabs
        value={activeTab}
        onValueChange={setActiveTab}
        className="w-full gap-0"
      >
        {/* Tier 1: Primary Header Bar */}
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border/60 px-4 py-2.5">
          <TabsList className="h-8 bg-muted/50 p-0.5">
            <TabsTrigger
              value="trajectory"
              className="gap-1.5 px-3 py-1 text-[12px] font-medium data-[state=active]:bg-background data-[state=active]:text-foreground"
            >
              <span>📈</span>
              <span>Net Worth Trajectory</span>
            </TabsTrigger>
            <TabsTrigger
              value="waterfall"
              className="gap-1.5 px-3 py-1 text-[12px] font-medium data-[state=active]:bg-background data-[state=active]:text-foreground"
            >
              <span>🌊</span>
              <span>Cashflow Waterfall</span>
            </TabsTrigger>
          </TabsList>

          <Link
            href="/portfolio"
            className="inline-flex items-center gap-1 text-[11.5px] font-medium text-muted-foreground transition-colors hover:text-foreground"
          >
            <span>Portfolio analytics</span>
            <span className="text-[12px]">↗</span>
          </Link>
        </div>

        {/* Tier 2: Sub-Tier Contextual Controls Bar */}
        <div className="flex flex-wrap items-center justify-between gap-2.5 border-b border-border/40 bg-muted/15 px-4 py-2 text-[11.5px]">
          {activeTab === "trajectory" ? (
            <>
              {/* Left: View Composition Toggle (Total | Breakdown) */}
              <div className="flex items-center rounded-md border border-border/60 bg-muted/40 p-0.5">
                <Button
                  variant="ghost"
                  size="sm"
                  aria-pressed={composition === "total"}
                  onClick={() => setComposition("total")}
                  className={cn(
                    "h-6 px-2.5 text-[11px] font-medium transition-colors",
                    composition === "total"
                      ? "bg-background text-foreground shadow-xs font-semibold"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  Total
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  aria-pressed={composition === "breakdown"}
                  onClick={() => setComposition("breakdown")}
                  className={cn(
                    "h-6 px-2.5 text-[11px] font-medium transition-colors",
                    composition === "breakdown"
                      ? "bg-background text-foreground shadow-xs font-semibold"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  Breakdown
                </Button>
              </div>

              {/* Right: Range Filter Pills */}
              <div className="flex items-center gap-1">
                {RANGES.map((r) => (
                  <Button
                    key={r}
                    variant="ghost"
                    size="sm"
                    aria-pressed={range === r}
                    onClick={() => setRange(r)}
                    className={cn(
                      "h-6 px-2 text-[10.5px] font-medium transition-colors",
                      range === r
                        ? "bg-primary text-primary-foreground font-semibold"
                        : "text-muted-foreground hover:bg-muted hover:text-foreground",
                    )}
                  >
                    {r}
                  </Button>
                ))}
              </div>
            </>
          ) : (
            <>
              {/* Left: Scope Toggle (Calendar CY vs Fiscal FY) */}
              <div className="flex items-center rounded-md border border-border/60 bg-muted/40 p-0.5">
                <Button
                  variant="ghost"
                  size="sm"
                  aria-pressed={yearType === "calendar"}
                  onClick={() => setYearType("calendar")}
                  className={cn(
                    "h-6 px-2.5 text-[11px] font-medium transition-colors",
                    yearType === "calendar"
                      ? "bg-background text-foreground shadow-xs font-semibold"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  Calendar (CY)
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  aria-pressed={yearType === "fiscal"}
                  onClick={() => setYearType("fiscal")}
                  className={cn(
                    "h-6 px-2.5 text-[11px] font-medium transition-colors",
                    yearType === "fiscal"
                      ? "bg-background text-foreground shadow-xs font-semibold"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  Fiscal (FY)
                </Button>
              </div>

              {/* Right: Year Picker Dropdown */}
              <div className="flex items-center gap-2">
                <select
                  value={effectiveYear}
                  onChange={(e) => setSelectedYear(Number(e.target.value))}
                  className="h-7 rounded-md border border-input bg-background px-2 py-0.5 text-[11.5px] font-medium text-foreground shadow-xs focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                >
                  {yearsQuery.years.map((y) => (
                    <option key={y} value={y}>
                      {yearType === "fiscal" ? `FY ${y}-${(y + 1) % 100}` : y}
                    </option>
                  ))}
                </select>
              </div>
            </>
          )}
        </div>

        {/* Tab 1: Net Worth Trajectory */}
        <TabsContent value="trajectory" className="p-0">
          <NetWorthCard
            overview={overview}
            embedded
            composition={composition}
            range={range}
            onRangeChange={setRange}
            onCompositionChange={setComposition}
          />
        </TabsContent>

        {/* Tab 2: Cashflow Waterfall */}
        <TabsContent value="waterfall" className="p-0">
          <CashflowWaterfallCard
            embedded
            yearType={yearType}
            year={effectiveYear}
            onYearTypeChange={setYearType}
            onYearChange={setSelectedYear}
          />
        </TabsContent>
      </Tabs>
    </section>
  );
}
