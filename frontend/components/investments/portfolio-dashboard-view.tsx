"use client";

/**
 * Portfolio Performance & Valuation Dashboard View (Milestone 2 Phase 5).
 *
 * Primary interactive view uniting:
 * 1. Scope & Benchmark selection
 * 2. Key Performance Indicators stat tiles
 * 3. Unitized TWR vs. Benchmark TRI performance chart
 * 4. Portfolio Valuation & Net Invested Capital area chart
 * 5. Rolling period breakdown matrix table
 *
 * Handles loading skeletons, empty state for fresh accounts, and reactive period syncing.
 */

import { useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";

import { listBenchmarks } from "@/lib/api/client";
import { getInvestmentAccounts } from "@/lib/api/investment-accounts";
import {
  usePortfolioTimeseries,
  type ScopeType,
} from "@/lib/api/portfolio-timeseries";
import type { RollingPeriod } from "@/lib/investments";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { AllocationTargetCard } from "@/src/components/portfolio/allocation-target-card";
import { PortfolioSummary } from "@/app/portfolio/portfolio-summary";
import { PortfolioPerformanceChart } from "./portfolio-performance-chart";
import { PortfolioScopeBenchmarkBar } from "./portfolio-scope-benchmark-bar";
import { PortfolioStatTiles } from "./portfolio-stat-tiles";
import { RollingPeriodTable } from "./rolling-period-table";
import { ValuationCapitalChart } from "./valuation-capital-chart";

export function PortfolioDashboardView() {
  const [scope, setScope] = useState<ScopeType>("all");
  const [selectedAccountId, setSelectedAccountId] = useState<number | null>(null);
  const [selectedBenchmarkId, setSelectedBenchmarkId] = useState<number | null>(null);
  const [selectedPeriod, setSelectedPeriod] = useState<RollingPeriod>("all");
  const [activeTab, setActiveTab] = useState<"performance" | "allocation" | "rebalance">("performance");

  // 1. Fetch active investment accounts
  const accountsQuery = useQuery({
    queryKey: ["investment-accounts", { is_active: true }],
    queryFn: () => getInvestmentAccounts({ is_active: true }),
  });
  const accounts = accountsQuery.data ?? [];

  // 2. Fetch active benchmarks
  const benchmarksQuery = useQuery({
    queryKey: ["benchmarks"],
    queryFn: listBenchmarks,
  });
  const benchmarks = benchmarksQuery.data ?? [];

  // 3. Fetch portfolio timeseries data
  const timeseriesQuery = usePortfolioTimeseries({
    scope,
    account_id: scope === "account" ? selectedAccountId : undefined,
    benchmark_id: selectedBenchmarkId ?? undefined,
  });

  const handleScopeChange = (newScope: ScopeType, accountId?: number | null) => {
    setScope(newScope);
    setSelectedAccountId(accountId ?? null);
  };

  const handleBenchmarkChange = (benchmarkId: number) => {
    setSelectedBenchmarkId(benchmarkId);
  };

  const timeseries = timeseriesQuery.data;
  const points = timeseries?.points ?? [];
  const periods = timeseries?.periods ?? [];
  const benchmarkName = timeseries?.benchmark_name ?? "Benchmark";

  const isPending = timeseriesQuery.isPending;
  const isError = timeseriesQuery.isError;
  const isEmpty = !isPending && !isError && points.length === 0;

  return (
    <div className="flex flex-col gap-6">
      {/* 1. Scope & Benchmark Controls */}
      <PortfolioScopeBenchmarkBar
        scope={scope}
        selectedAccountId={selectedAccountId}
        accounts={accounts}
        benchmarks={benchmarks}
        selectedBenchmarkId={selectedBenchmarkId ?? timeseries?.benchmark_id ?? null}
        onScopeChange={handleScopeChange}
        onBenchmarkChange={handleBenchmarkChange}
        isLoading={isPending}
      />

      {/* Error state */}
      {isError ? (
        <Card className="border-neg/30 bg-neg/5">
          <CardContent className="p-6 text-center text-sm text-neg">
            Couldn’t load portfolio timeseries data. Please verify the API is running.
          </CardContent>
        </Card>
      ) : null}

      {/* Loading state skeletons */}
      {isPending ? (
        <div className="flex flex-col gap-6 animate-pulse">
          {/* Stat tiles skeleton */}
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            {[1, 2, 3, 4].map((i) => (
              <div
                key={i}
                className="h-24 rounded-lg border border-border bg-muted/40"
              />
            ))}
          </div>

          {/* Charts skeletons */}
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            <div className="h-[360px] rounded-xl border border-border bg-muted/30" />
            <div className="h-[360px] rounded-xl border border-border bg-muted/30" />
          </div>

          {/* Table skeleton */}
          <div className="h-[260px] rounded-xl border border-border bg-muted/20" />
        </div>
      ) : null}

      {/* Empty state for fresh accounts / no transactions */}
      {isEmpty ? (
        <Card className="border-dashed border-border py-12">
          <CardContent className="flex flex-col items-center justify-center text-center gap-3">
            <div className="size-10 rounded-full bg-muted flex items-center justify-center text-muted-foreground text-lg">
              📈
            </div>
            <div className="space-y-1">
              <h3 className="font-semibold text-base">No investment history recorded</h3>
              <p className="text-[13px] text-muted-foreground max-w-md">
                Record your first transaction or import a statement to view unitized TWR
                performance, benchmark alpha comparison, and daily valuation history.
              </p>
            </div>
            <div className="flex items-center gap-3 mt-2">
              <Button asChild size="sm">
                <Link href="/investments">Record Transaction</Link>
              </Button>
              <Button asChild variant="outline" size="sm">
                <Link href="/imports/investments">Import Statements</Link>
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : null}

      {/* Live Content */}
      {!isPending && !isError && !isEmpty ? (
        <div className="flex flex-col gap-6">
          {/* 2. Key Performance Indicators Stat Tiles */}
          <PortfolioStatTiles
            points={points}
            periods={periods}
            selectedPeriod={selectedPeriod}
            benchmarkName={benchmarkName}
            isLoading={isPending}
          />

          {/* 3. Segmented Workspace Tabs */}
          <div className="flex items-center gap-2 border-b border-border/60 pb-3">
            <button
              type="button"
              onClick={() => setActiveTab("performance")}
              className={cn(
                "inline-flex items-center gap-2 rounded-lg px-3.5 py-1.5 text-xs font-medium transition-colors",
                activeTab === "performance"
                  ? "bg-primary text-primary-foreground shadow-sm"
                  : "bg-muted/50 text-muted-foreground hover:bg-muted hover:text-foreground",
              )}
            >
              <span>📊</span>
              <span>Performance & Valuation</span>
            </button>
            <button
              type="button"
              onClick={() => setActiveTab("allocation")}
              className={cn(
                "inline-flex items-center gap-2 rounded-lg px-3.5 py-1.5 text-xs font-medium transition-colors",
                activeTab === "allocation"
                  ? "bg-primary text-primary-foreground shadow-sm"
                  : "bg-muted/50 text-muted-foreground hover:bg-muted hover:text-foreground",
              )}
            >
              <span>🎯</span>
              <span>Asset Allocation & Drift</span>
            </button>
            <button
              type="button"
              onClick={() => setActiveTab("rebalance")}
              className={cn(
                "inline-flex items-center gap-2 rounded-lg px-3.5 py-1.5 text-xs font-medium transition-colors",
                activeTab === "rebalance"
                  ? "bg-primary text-primary-foreground shadow-sm"
                  : "bg-muted/50 text-muted-foreground hover:bg-muted hover:text-foreground",
              )}
            >
              <span>⚖️</span>
              <span>Rebalance Desk</span>
            </button>
          </div>

          {/* Tab 1: Performance & Valuation */}
          {activeTab === "performance" && (
            <div className="flex flex-col gap-6">
              {/* Interactive Charts Grid (Performance TWR vs. Valuation & Capital) */}
              <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
                <PortfolioPerformanceChart
                  points={points}
                  periods={periods}
                  benchmarkName={benchmarkName}
                  selectedPeriod={selectedPeriod}
                  onSelectPeriod={setSelectedPeriod}
                />

                <ValuationCapitalChart
                  points={points}
                  periods={periods}
                  benchmarkName={benchmarkName}
                  selectedPeriod={selectedPeriod}
                />
              </div>

              {/* Rolling Period Performance Matrix */}
              <RollingPeriodTable
                periods={periods}
                selectedPeriod={selectedPeriod}
                onSelectPeriod={setSelectedPeriod}
                benchmarkName={benchmarkName}
              />
            </div>
          )}

          {/* Tab 2: Asset Allocation & Drift */}
          {activeTab === "allocation" && (
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
              <div className="lg:col-span-5">
                <PortfolioSummary hideTiles={true} />
              </div>
              <div className="lg:col-span-7">
                <AllocationTargetCard
                  scope={scope === "account" ? "account" : "all"}
                  accountId={selectedAccountId}
                  onScopeChange={(newScope, newAccId) => handleScopeChange(newScope, newAccId)}
                  hideScopeSelector={true}
                />
              </div>
            </div>
          )}

          {/* Tab 3: Rebalance Desk */}
          {activeTab === "rebalance" && (
            <div className="flex flex-col gap-6">
              <AllocationTargetCard
                scope={scope === "account" ? "account" : "all"}
                accountId={selectedAccountId}
                onScopeChange={(newScope, newAccId) => handleScopeChange(newScope, newAccId)}
                hideScopeSelector={true}
              />
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
