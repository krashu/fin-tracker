"use client";

/**
 * Tier 3: Macro Wealth Horizon Deck (PRD §F8 View 4 & Strategic Wealth Horizon).
 *
 * Consolidates low-frequency, strategic wealth visualizers into an elegant tabbed deck:
 * - Tab 1: 📈 Net Worth Trajectory (stacked area chart across 7 asset classes, milestones, 1M-ALL filters)
 * - Tab 2: 🌊 Cashflow Waterfall (annual conversion of cash inflows into living expenses, peer dues, investments, surplus)
 *
 * Preserves 100% of the analytical charts while saving ~750px of vertical height.
 */

import Link from "next/link";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { NetWorthCard } from "@/components/dashboard/net-worth-card";
import { CashflowWaterfallCard } from "@/components/dashboard/cashflow-waterfall-card";
import { type OverviewResponse } from "@/lib/api/client";

interface MacroWealthDeckProps {
  overview?: OverviewResponse;
}

export function MacroWealthDeck({ overview }: MacroWealthDeckProps) {
  return (
    <section aria-label="Macro Wealth Horizon" className="overflow-hidden rounded-lg border border-border bg-card">
      <Tabs defaultValue="trajectory" className="w-full gap-0">
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

        <TabsContent value="trajectory" className="p-0">
          <NetWorthCard overview={overview} embedded />
        </TabsContent>

        <TabsContent value="waterfall" className="p-0">
          <CashflowWaterfallCard embedded />
        </TabsContent>
      </Tabs>
    </section>
  );
}
