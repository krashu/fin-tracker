"use client";

/**
 * /expenses filter chips — account / category / type plus a MONTH navigator.
 * Presentational: the board (expenses-board.tsx) owns the values + change
 * handlers; this just renders the controls and emits selections.
 *
 * The date control is a month stepper (◀ June 2026 ▶), defaulting to the
 * current month, with an "All dates" escape hatch. Stepping picks that calendar
 * month (and turns All-dates off); the board turns the anchor into a
 * `{date_from,date_to}` range via `monthRange`. The stepper is dimmed while
 * All-dates is active but stays clickable — it's the way back to a single month.
 */
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Pill } from "@/components/ui/pill";
import { IconChevronDown, IconChevronRight, IconX } from "@/components/icons";
import { formatMonth } from "@/lib/format";
import { labelDisplay } from "@/lib/labels";
import { cn } from "@/lib/utils";
import type {
  AccountRead,
  CategoryRead,
  LabelRead,
  TransactionType,
} from "@/lib/api/client";

/** The board's transaction-type view. "spending" is its default identity
 * (spend + refund — refunds net against spend per §F4a); "income" and
 * "transfers" are the opt-in views. Transfers are *entered* through their own
 * dialog, but they are browsable here — otherwise a mis-detected F4a link or a
 * stray manual transfer would be unreachable from the UI, with no way to
 * inspect or delete it. */
export type TypeFilter = "spending" | "refunds" | "income" | "transfers";

const TYPE_FILTER_LABEL: Record<TypeFilter, string> = {
  spending: "Spending",
  refunds: "Refunds",
  income: "Income",
  transfers: "Transfers",
};

/** The `transaction_type` set each view requests. Always concrete — never omits
 * the param, since omitting it widens the list endpoint to *every* type at once
 * (transfers mixed into the spending list), which no view wants. */
export function typeFilterToParam(t: TypeFilter): TransactionType[] {
  if (t === "refunds") return ["refund"];
  if (t === "income") return ["income"];
  if (t === "transfers") return ["transfer"];
  return ["spend"];
}

export type FilterRowProps = {
  accounts: AccountRead[];
  categories: CategoryRead[];
  labels: LabelRead[];
  typeFilter: TypeFilter;
  accountId?: number;
  categoryId?: number;
  labelId?: number;
  year: number;
  availableYears: number[];
  monthAnchor: Date;
  allDates: boolean;
  atCurrentMonth: boolean;
  hasActiveFilters: boolean;
  onClearFilters: () => void;
  onTypeFilterChange: (t: TypeFilter) => void;
  onAccountChange: (id: number | undefined) => void;
  onCategoryChange: (id: number | undefined) => void;
  onLabelChange: (id: number | undefined) => void;
  onYearChange: (year: number) => void;
  onStepMonth: (delta: number) => void;
  onToggleAllDates: () => void;
};

export function FilterRow({
  accounts,
  categories,
  labels,
  typeFilter,
  accountId,
  categoryId,
  labelId,
  year,
  availableYears,
  monthAnchor,
  allDates,
  atCurrentMonth,
  hasActiveFilters,
  onClearFilters,
  onTypeFilterChange,
  onAccountChange,
  onCategoryChange,
  onLabelChange,
  onYearChange,
  onStepMonth,
  onToggleAllDates,
}: FilterRowProps) {
  const account = accounts.find((a) => a.id === accountId);
  // Lookup over the full list (active pill label); options are kind-filtered to
  // match the type view (spending and transfers → spend categories, income →
  // income). Transfers keep the spend picker deliberately: a transfer may carry
  // a spend category (ADR-0007 rule 7), so hiding it would strand those rows.
  const category = categories.find((c) => c.id === categoryId);
  const categoryKind =
    typeFilter === "income"
      ? "income"
      : typeFilter === "refunds"
      ? "refund"
      : "spend";
  const visibleCategories = categories.filter((c) => c.kind === categoryKind);
  const label = labels.find((l) => l.id === labelId);

  return (
    <div className="sticky top-[64px] z-20 flex flex-wrap items-center gap-2 bg-background pt-4 pb-3">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Pill active={typeFilter !== "spending"}>
            {`Type: ${TYPE_FILTER_LABEL[typeFilter]}`}
            <IconChevronDown className="size-3 opacity-70" />
          </Pill>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="max-h-72 w-48">
          {(["spending", "refunds", "income", "transfers"] as const).map((t) => (
            <DropdownMenuItem key={t} onSelect={() => onTypeFilterChange(t)}>
              {t === "spending" ? (
                <span className="text-muted-foreground">
                  {TYPE_FILTER_LABEL[t]}
                </span>
              ) : (
                TYPE_FILTER_LABEL[t]
              )}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>

      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Pill active={accountId != null}>
            {account ? account.name : "All accounts"}
            <IconChevronDown className="size-3 opacity-70" />
          </Pill>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="max-h-72 w-48">
          <DropdownMenuItem onSelect={() => onAccountChange(undefined)}>
            <span className="text-muted-foreground">All accounts</span>
          </DropdownMenuItem>
          {accounts.map((a) => (
            <DropdownMenuItem key={a.id} onSelect={() => onAccountChange(a.id)}>
              {a.name}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>

      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Pill active={categoryId != null}>
            {category ? category.name : "All categories"}
            <IconChevronDown className="size-3 opacity-70" />
          </Pill>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="max-h-72 w-48">
          <DropdownMenuItem onSelect={() => onCategoryChange(undefined)}>
            <span className="text-muted-foreground">All categories</span>
          </DropdownMenuItem>
          {visibleCategories.map((c) => (
            <DropdownMenuItem
              key={c.id}
              onSelect={() => onCategoryChange(c.id)}
            >
              {c.name}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>

      {/* Tag filter — single-label (EXISTS on the join). Hidden until the user
          has at least one tag, since the catalog starts empty (unlike the seeded
          accounts/categories). Not kind-scoped: tags are cross-cutting. */}
      {labels.length > 0 ? (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Pill active={labelId != null}>
              {label ? labelDisplay(label.name) : "All tags"}
              <IconChevronDown className="size-3 opacity-70" />
            </Pill>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="max-h-72 w-48">
            <DropdownMenuItem onSelect={() => onLabelChange(undefined)}>
              <span className="text-muted-foreground">All tags</span>
            </DropdownMenuItem>
            {labels.map((l) => (
              <DropdownMenuItem key={l.id} onSelect={() => onLabelChange(l.id)}>
                {labelDisplay(l.name)}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      ) : null}

      {/* Year selector — dropdown pill listing available transaction years. */}
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Pill active={year !== new Date().getFullYear()}>
            {year}
            <IconChevronDown className="size-3 opacity-70" />
          </Pill>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="max-h-72 w-32">
          {availableYears.map((y) => (
            <DropdownMenuItem key={y} onSelect={() => onYearChange(y)}>
              {y === new Date().getFullYear() ? (
                <span className="text-muted-foreground">{y}</span>
              ) : (
                String(y)
              )}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>

      {/* Month navigator — pill-height (h-7) so the row keeps its height and the
          table's sticky `stickyTop` offset stays valid. Dimmed but clickable
          while All-dates is active. */}
      <div
        className={cn(
          "inline-flex h-7 items-center gap-0.5 rounded-md border border-border bg-card px-0.5",
          allDates && "opacity-50",
        )}
      >
        <button
          type="button"
          onClick={() => onStepMonth(-1)}
          aria-label="Previous month"
          className="grid size-6 place-items-center rounded-[5px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <IconChevronRight className="size-3.5 rotate-180" />
        </button>
        <span className="min-w-[100px] text-center text-[12px] font-medium tabular-nums text-foreground">
          {formatMonth(monthAnchor)}
        </span>
        <button
          type="button"
          onClick={() => onStepMonth(1)}
          disabled={atCurrentMonth}
          aria-label="Next month"
          className="grid size-6 place-items-center rounded-[5px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-40"
        >
          <IconChevronRight className="size-3.5" />
        </button>
      </div>

      <Pill active={allDates} onClick={onToggleAllDates}>
        All months
      </Pill>

      {/* Appears only when a filter is off its default — resets everything in one
          click, so it doubles as a signal that filters are active. */}
      {hasActiveFilters ? (
        <Pill onClick={onClearFilters} aria-label="Clear all filters">
          <IconX className="size-3 opacity-70" />
          Clear
        </Pill>
      ) : null}
    </div>
  );
}
