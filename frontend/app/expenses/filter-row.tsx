"use client";

/**
 * /expenses filter chips — account / category / type plus a MONTH navigator.
 * Presentational: the board (expenses-board.tsx) owns the values + change
 * handlers; this just renders the controls and emits selections.
 *
 * The date control has three states, each wider than the last: a month
 * stepper (◀ June 2026 ▶), defaulting to the current month (the board turns
 * the anchor into a `{date_from,date_to}` range via `monthRange`); "All
 * months" widens that to every month of the selected year (`yearRange`);
 * "All years" widens it again to every date in the data — both bounds
 * omitted, so the year selector no longer applies either. Picking a year, or
 * stepping the month, always drops back to a single bounded month/year — the
 * way back from either escape hatch. The year selector and the month stepper
 * are dimmed while "All years" is active, and the stepper is also dimmed
 * while "All months" is active — all stay clickable, since they're the way
 * back.
 */
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Pill } from "@/components/ui/pill";
import { PeriodPicker } from "@/components/dashboard/period-picker";
import { IconChevronDown, IconChevronRight, IconX } from "@/components/icons";
import { formatMonth } from "@/lib/format";
import { labelDisplay } from "@/lib/labels";
import {
  buildCategoryTree,
  categoryLabel,
  resolveCategoryColor,
} from "@/lib/categories";
import { CategoryDot } from "@/components/category-dot";
import { cn } from "@/lib/utils";
import type {
  AccountRead,
  CategoryRead,
  LabelRead,
  ListTransactionsParams,
} from "@/lib/api/client";

/** The board's transaction-type view. "spending" is its default identity
 * (every `spend` row, either sign — refunds net against spend per §F4a);
 * "refunds", "income" and "transfers" are the opt-in views. Transfers are
 * *entered* through their own dialog, but they are browsable here — otherwise
 * a mis-detected F4a link or a stray manual transfer would be unreachable
 * from the UI, with no way to inspect or delete it. */
export type TypeFilter = "spending" | "refunds" | "income" | "transfers";

const TYPE_FILTER_LABEL: Record<TypeFilter, string> = {
  spending: "Spending",
  refunds: "Refunds",
  income: "Income",
  transfers: "Transfers",
};

/** The list-endpoint params each view requests. `transaction_type` is always
 * concrete — never omitted, since omitting it widens the list endpoint to
 * *every* type at once (transfers mixed into the spending list), which no view
 * wants. "refunds" is no longer its own `transaction_type` (ADR-0009 collapsed
 * it into a positively-signed `spend`), so it composes the orthogonal
 * `amount_sign` filter instead — `spend` rows, positive amount only. */
export function typeFilterToParam(
  t: TypeFilter,
): Pick<ListTransactionsParams, "transaction_type" | "amount_sign"> {
  if (t === "refunds") return { transaction_type: ["spend"], amount_sign: "positive" };
  if (t === "income") return { transaction_type: ["income"] };
  if (t === "transfers") return { transaction_type: ["transfer"] };
  return { transaction_type: ["spend"] };
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
  allYears: boolean;
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
  onToggleAllYears: () => void;
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
  allYears,
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
  onToggleAllYears,
}: FilterRowProps) {
  const account = accounts.find((a) => a.id === accountId);
  // The active pill label resolves over the full list via `categoryLabel` below;
  // options are kind-filtered to match the type view (spending and transfers →
  // spend categories, income → income). Transfers keep the spend picker
  // deliberately: a transfer may carry a spend category (ADR-0007 rule 7), so
  // hiding it would strand those rows.
  const categoryKind = typeFilter === "income" ? "income" : "spend";
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
            {/* Three states, not two. An ARCHIVED id is absent from `categories`
                (the list is active-only), and the old `category ? … : "All
                categories"` collapsed that onto the no-filter branch — so a
                drilldown into a just-archived category rendered an *active* pill
                reading "All categories" while the board was genuinely filtered.
                No stored name is available here (the chip resolves a bare id,
                with no transaction in hand), hence the generic fallback. */}
            {categoryId == null
              ? "All categories"
              : categoryLabel(categoryId, categories)}
            <IconChevronDown className="size-3 opacity-70" />
          </Pill>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="max-h-72 w-56 overflow-y-auto">
          <DropdownMenuItem onSelect={() => onCategoryChange(undefined)}>
            <span className="text-muted-foreground">All categories</span>
          </DropdownMenuItem>
          {buildCategoryTree(visibleCategories).map((parent) => (
            <div key={parent.id} className="py-0.5">
              <DropdownMenuItem
                onSelect={() => onCategoryChange(parent.id)}
                className="flex items-center gap-2 font-medium"
              >
                <CategoryDot
                  categoryId={parent.id}
                  color={resolveCategoryColor(parent, categories)}
                />
                <span className="truncate">{parent.name}</span>
              </DropdownMenuItem>
              {parent.subcategories.map((sub) => (
                <DropdownMenuItem
                  key={sub.id}
                  onSelect={() => onCategoryChange(sub.id)}
                  className="ml-3 flex items-center gap-2 border-l border-border/60 pl-3 text-[12px]"
                >
                  <CategoryDot
                    categoryId={sub.id}
                    color={resolveCategoryColor(sub, categories)}
                    className="size-1.5"
                  />
                  <span className="truncate">{sub.name}</span>
                </DropdownMenuItem>
              ))}
            </div>
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

      {/* Year selector. Year-only — the month navigator below is its own,
          separate control (a stepper, not this dropdown). Dimmed but still
          clickable while "All years" is active — picking a year is the way
          back to a single one. */}
      <div className={cn(allYears && "opacity-50")}>
        <PeriodPicker
          period={{ year }}
          availableYears={availableYears}
          onChange={(p) => onYearChange(p.year)}
          allowMonth={false}
        />
      </div>

      {/* Month navigator — pill-height (h-7) so the row keeps its height and the
          table's sticky `stickyTop` offset stays valid. Dimmed but clickable
          while All-dates is active. */}
      <div
        className={cn(
          "inline-flex h-7 items-center gap-0.5 rounded-md border border-border bg-card px-0.5",
          (allDates || allYears) && "opacity-50",
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

      <Pill active={allYears} onClick={onToggleAllYears}>
        All years
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
