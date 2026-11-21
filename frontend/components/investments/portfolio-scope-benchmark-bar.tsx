"use client";

/**
 * Scope & Benchmark Switcher Bar (Milestone 2 Phase 5).
 *
 * Controls:
 * - Scope filter: All Holdings, specific Demat / Folio / US Broker account, or Member
 * - Benchmark dropdown: Toggle between Nifty 50 TRI, S&P 500, etc.
 */

import { PickerButton } from "@/components/form/fields";
import { Badge } from "@/components/ui/badge";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { Pill } from "@/components/ui/pill";
import type { Benchmark } from "@/lib/api/client";
import type { ScopeType } from "@/lib/api/portfolio-timeseries";
import type { InvestmentAccountRead } from "@/lib/types/investment-accounts";
import { cn } from "@/lib/utils";

export type PortfolioScopeBenchmarkBarProps = {
  scope: ScopeType;
  selectedAccountId: number | null;
  accounts: InvestmentAccountRead[];
  benchmarks: Benchmark[];
  selectedBenchmarkId: number | null;
  onScopeChange: (scope: ScopeType, accountId?: number | null) => void;
  onBenchmarkChange: (benchmarkId: number) => void;
  isLoading?: boolean;
  className?: string;
};

export function PortfolioScopeBenchmarkBar({
  scope,
  selectedAccountId,
  accounts,
  benchmarks,
  selectedBenchmarkId,
  onScopeChange,
  onBenchmarkChange,
  isLoading = false,
  className,
}: PortfolioScopeBenchmarkBarProps) {
  const isAll = scope === "all" || (scope === "account" && selectedAccountId === null);
  const selectedBenchmark =
    benchmarks.find((b) => b.id === selectedBenchmarkId) ?? benchmarks[0] ?? null;

  return (
    <div
      className={cn(
        "flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between py-2 border-b border-border/40 pb-3",
        className,
      )}
    >
      {/* Scope Selector: All, Accounts, Member */}
      <div className="flex flex-wrap items-center gap-1.5 overflow-x-auto">
        <span className="text-[11.5px] font-medium text-muted-foreground mr-1">
          Scope:
        </span>

        {/* All Holdings */}
        <Pill
          active={isAll}
          onClick={() => onScopeChange("all", null)}
          className="cursor-pointer gap-1.5"
          aria-pressed={isAll}
        >
          <span>All Holdings</span>
          {accounts.length > 0 ? (
            <Badge
              variant={isAll ? "secondary" : "default"}
              size="sm"
              className="text-[10px] px-1"
            >
              {accounts.length}
            </Badge>
          ) : null}
        </Pill>

        {/* Individual Accounts */}
        {accounts.map((acc) => {
          const isSelected = scope === "account" && selectedAccountId === acc.id;
          return (
            <Pill
              key={acc.id}
              active={isSelected}
              onClick={() => onScopeChange("account", acc.id)}
              className="cursor-pointer gap-1.5"
              aria-pressed={isSelected}
            >
              <span>{acc.name}</span>
              <Badge
                variant="outline"
                size="sm"
                className={cn(
                  "text-[9.5px] px-1",
                  isSelected
                    ? "border-accent-foreground/30 text-accent-foreground"
                    : "border-border text-muted-foreground",
                )}
              >
                {acc.account_type === "demat"
                  ? "Demat"
                  : acc.account_type === "mf_folio"
                    ? "MF"
                    : acc.account_type === "us_broker"
                      ? "US"
                      : acc.account_type}
              </Badge>
            </Pill>
          );
        })}

        {/* Family Member Scope Option */}
        <Pill
          active={scope === "member"}
          onClick={() => onScopeChange("member", null)}
          className="cursor-pointer"
          aria-pressed={scope === "member"}
        >
          <span>Family (Self)</span>
        </Pill>
      </div>

      {/* Benchmark Dropdown */}
      <div className="flex items-center gap-2 shrink-0">
        <span className="text-[11.5px] font-medium text-muted-foreground whitespace-nowrap">
          Benchmark:
        </span>
        <div className="w-48">
          <PickerButton
            label={selectedBenchmark?.name ?? "Nifty 50"}
            muted={isLoading}
          >
            {benchmarks.map((b) => (
              <DropdownMenuItem
                key={b.id}
                onSelect={() => onBenchmarkChange(b.id)}
                className="cursor-pointer"
              >
                <div className="flex items-center justify-between w-full">
                  <span>{b.name}</span>
                  {b.id === selectedBenchmark?.id ? (
                    <span className="text-primary text-xs">✓</span>
                  ) : null}
                </div>
              </DropdownMenuItem>
            ))}
          </PickerButton>
        </div>
      </div>
    </div>
  );
}
