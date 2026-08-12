"use client";

/**
 * Live expenses table — the data-driven half of /expenses. The page shell owns
 * the heading; this client island fetches and renders.
 *
 * Reads three endpoints and joins client-side: TransactionRead is flat (FK ids
 * only), so account name+last4 and the category label come from accountsById /
 * categoriesById lookups. The Type filter scopes the list to Spending (every
 * `spend` row, either sign — the default), Refunds, Income, or Transfers — one
 * concrete `{transaction_type, amount_sign?}` param set per view, never
 * omitted (server-side filter keeps pagination honest); see
 * `typeFilterToParam`.
 *
 * Amount sign keys off the *stored* sign (PRD §F4a source of truth): the
 * backend stores spend negative, refund positive (a refund IS a `spend` row —
 * ADR-0009), so a positive amount renders as a green credit and a negative as
 * a spend magnitude. Render does not key off transaction_type — sign is the
 * contract.
 */
import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { Checkbox } from "@/components/ui/checkbox";
import { useAvailableYears } from "@/components/dashboard/use-available-years";
import {
  listAccounts,
  listCategories,
  listLabels,
  listTransactions,
  type AccountRead,
  type CategoryColor,
  type CategoryRead,
  type LabelRead,
  type TransactionRead,
} from "@/lib/api/client";
import { accountLabel } from "@/lib/accounts";
import { formatINR, formatDate } from "@/lib/format";
import { thisMonthAnchor, monthKey, monthRange, yearRange } from "@/lib/dates";
import { cn } from "@/lib/utils";
import { CategoryDot } from "@/components/category-dot";
import { LabelChip } from "@/components/labels/label-chip";
import { Sensitive } from "@/components/balance-visibility";
import { StateRow, Td, Th } from "@/components/ui/table";
import { SummaryStrip } from "@/components/dashboard/summary-strip";
import { TransactionDialog } from "./transaction-dialog";
import { FilterRow, typeFilterToParam, type TypeFilter } from "./filter-row";
import { SelectionBar } from "./selection-bar";

const PAGE_SIZE = 50;

/** Parse a positive-integer id from a URL param — anything else (float, zero,
 * negative, "1e3", junk) is treated as absent. */
function parseIdParam(value: string | null): number | undefined {
  if (value == null) return undefined;
  const n = Number(value);
  return Number.isInteger(n) && n > 0 ? n : undefined;
}

export function ExpensesBoard() {
  const [selectedTxn, setSelectedTxn] = useState<TransactionRead | null>(null);

  // The ⌘K palette deep-links here via ?account=<id> / ?category=<id>.
  const searchParams = useSearchParams();
  const urlAccountId = parseIdParam(searchParams.get("account"));
  const urlCategoryId = parseIdParam(searchParams.get("category"));

  // Filters (type view, account/category by id, month navigator) + bulk selection.
  // `monthAnchor` is always a real first-of-month (drives the stepper); `allDates`
  // is the escape hatch that drops the month range. Default: the current month.
  // account/category seed from the URL (deep-link on a fresh mount); the effects
  // below keep them in sync when the palette re-navigates on the same route.
  const [typeFilter, setTypeFilter] = useState<TypeFilter>("spending");
  const [accountId, setAccountId] = useState<number | undefined>(urlAccountId);
  const [categoryId, setCategoryId] = useState<number | undefined>(
    urlCategoryId,
  );
  const [labelId, setLabelId] = useState<number | undefined>(undefined);
  const [monthAnchor, setMonthAnchor] = useState(thisMonthAnchor);
  const [allDates, setAllDates] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  // Derived, not its own state: onYearChange keeps monthAnchor's year in sync
  // (below), so a separate `year` state was purely redundant — and updating it
  // from inside onStepMonth's setMonthAnchor updater was an impure nested
  // setState (React requires updaters to be pure).
  const year = monthAnchor.getFullYear();

  // Next.js doesn't remount /expenses on a query-string-only change, so a one-
  // time seed isn't enough — a palette pick made while already on /expenses
  // would update the URL but not the filter. Re-sync when the parsed param
  // VALUE changes. Depending on the parsed value (not the searchParams object)
  // means a manual filter edit — which never touches the URL — won't refire and
  // clobber the user's choice. Known minor edge: re-picking the same id from the
  // palette after manually clearing it won't refire (params unchanged); fine for v1.
  useEffect(() => {
    setAccountId(urlAccountId);
    setSelectedIds(new Set());
  }, [urlAccountId]);
  useEffect(() => {
    setCategoryId(urlCategoryId);
    setSelectedIds(new Set());
  }, [urlCategoryId]);

  // Fetch available years for the year dropdown.
  const { years: availableYears } = useAvailableYears();

  // Cap forward stepping at the current month — no future spend to browse.
  const atCurrentMonth = monthKey(monthAnchor) === monthKey(thisMonthAnchor());
  // Stable string for the query key (never a raw Date — TanStack identity-hashes
  // objects) and the human-readable "scope" of what's loaded.
  const monthScope = allDates ? `year-${year}` : monthKey(monthAnchor);

  // "Active" = any filter off its default view (Spending · all accounts · all
  // categories · current month · current year). Drives the Clear pill's visibility.
  const currentYear = new Date().getFullYear();
  const hasActiveFilters =
    typeFilter !== "spending" ||
    accountId != null ||
    categoryId != null ||
    labelId != null ||
    allDates ||
    year !== currentYear ||
    !atCurrentMonth;

  // Any filter change drops the selection (a selected row may not survive the
  // new filter). Paging resets on its own: the filters ride the query key, so a
  // change starts a fresh useInfiniteQuery from offset 0.
  function resetView() {
    setSelectedIds(new Set());
  }

  // Reset every filter to its default view in one click.
  function clearFilters() {
    setTypeFilter("spending");
    setAccountId(undefined);
    setCategoryId(undefined);
    setLabelId(undefined);
    setMonthAnchor(thisMonthAnchor());
    setAllDates(false);
    resetView();
  }

  const txnsQuery = useInfiniteQuery({
    queryKey: [
      "transactions",
      {
        typeFilter,
        account_id: accountId,
        category_id: categoryId,
        label_id: labelId,
        month: monthScope,
      },
    ],
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      listTransactions({
        ...typeFilterToParam(typeFilter),
        limit: PAGE_SIZE,
        offset: pageParam,
        account_id: accountId,
        category_id: categoryId,
        label_id: labelId,
        ...(allDates ? yearRange(year) : monthRange(monthAnchor)),
      }),
    getNextPageParam: (lastPage, pages) =>
      lastPage.length === PAGE_SIZE ? pages.length * PAGE_SIZE : undefined,
  });
  const accountsQuery = useQuery({
    queryKey: ["accounts"],
    queryFn: listAccounts,
  });
  const categoriesQuery = useQuery({
    queryKey: ["categories"],
    queryFn: listCategories,
  });
  const labelsQuery = useQuery({
    queryKey: ["labels"],
    queryFn: listLabels,
  });

  const accountsById = new Map<number, AccountRead>(
    (accountsQuery.data ?? []).map((a) => [a.id, a]),
  );
  const categoriesById = new Map<number, CategoryRead>(
    (categoriesQuery.data ?? []).map((c) => [c.id, c]),
  );

  // Pages append as the sentinel scrolls into view; the flat list is what the
  // table and selection logic see.
  const rows = txnsQuery.data?.pages.flat() ?? [];
  const { fetchNextPage, hasNextPage, isFetchingNextPage } = txnsQuery;

  // Effective selection = selected ∩ loaded, recomputed at render (no effect):
  // after a bulk op invalidates + refetches, deleted ids simply drop out, so a
  // stale id can never inflate the count or act on an unloaded row.
  const loadedIds = new Set(rows.map((t) => t.id));
  const selectedLoadedIds = [...selectedIds].filter((id) => loadedIds.has(id));
  const allLoadedSelected =
    rows.length > 0 && selectedLoadedIds.length === rows.length;

  // Sentinel just below the last row: when it enters the viewport, pull the next
  // page. Guarded so we never stack fetches or page past the end.
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const node = sentinelRef.current;
    if (!node) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries[0]?.isIntersecting && hasNextPage && !isFetchingNextPage) {
        fetchNextPage();
      }
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [fetchNextPage, hasNextPage, isFetchingNextPage]);

  function toggleRow(id: number) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <>
      <SummaryStrip year={year} monthAnchor={monthAnchor} allDates={allDates} />

      <FilterRow
        accounts={accountsQuery.data ?? []}
        categories={categoriesQuery.data ?? []}
        labels={labelsQuery.data ?? []}
        typeFilter={typeFilter}
        accountId={accountId}
        categoryId={categoryId}
        labelId={labelId}
        year={year}
        availableYears={availableYears}
        monthAnchor={monthAnchor}
        allDates={allDates}
        atCurrentMonth={atCurrentMonth}
        hasActiveFilters={hasActiveFilters}
        onClearFilters={clearFilters}
        onTypeFilterChange={(t) => {
          setTypeFilter(t);
          // Switching the type view swaps the category scope — drop a selected
          // category that no longer belongs to the new kind.
          const nextKind = t === "income" ? "income" : "spend";
          if (
            categoryId != null &&
            categoriesById.get(categoryId)?.kind !== nextKind
          ) {
            setCategoryId(undefined);
          }
          resetView();
        }}
        onAccountChange={(id) => {
          setAccountId(id);
          resetView();
        }}
        onCategoryChange={(id) => {
          setCategoryId(id);
          resetView();
        }}
        onLabelChange={(id) => {
          setLabelId(id);
          resetView();
        }}
        onYearChange={(y) => {
          // Keep the monthAnchor's month index but move it to the new year —
          // `year` (derived above) follows automatically on the next render.
          // If the current month is in the future for the selected year,
          // clamp to December.
          const curMonth = monthAnchor.getMonth();
          const now = new Date();
          const maxMonth = y === now.getFullYear() ? now.getMonth() : 11;
          const newMonth = Math.min(curMonth, maxMonth);
          setMonthAnchor(new Date(y, newMonth, 1));
          setAllDates(false);
          resetView();
        }}
        onStepMonth={(delta) => {
          // Stepping picks a concrete month → leave All-dates mode. A pure
          // updater — crossing a year boundary just changes what `monthAnchor`
          // holds, and the derived `year` above reflects that on the next
          // render with no separate setState needed.
          setMonthAnchor((a) => new Date(a.getFullYear(), a.getMonth() + delta, 1));
          setAllDates(false);
          resetView();
        }}
        onToggleAllDates={() => {
          setAllDates((v) => !v);
          resetView();
        }}
      />

      <section className="rounded-lg border border-border bg-card">
        {/* Long list with a sticky header: `lg:contents` drops this wrapper at
            lg+ (where the 760px table fits) so the header keeps pinning to the
            viewport; below lg it scrolls horizontally inside the card instead of
            overflowing the page (an overflow container would capture the sticky). */}
        <div className="max-lg:overflow-x-auto lg:contents">
          <table className="w-full min-w-[920px] border-separate border-spacing-0">
            <colgroup>
              <col style={{ width: 40 }} />
              <col style={{ width: 92 }} />
              <col />
              <col style={{ width: 200 }} />
              <col style={{ width: 160 }} />
              <col style={{ width: 168 }} />
              <col style={{ width: 168 }} />
            </colgroup>
            <thead>
              <tr>
                <Th first stickyTop={112}>
                  <Checkbox
                    aria-label="Select all"
                    checked={
                      allLoadedSelected
                        ? true
                        : selectedLoadedIds.length > 0
                          ? "indeterminate"
                          : false
                    }
                    disabled={rows.length === 0}
                    onCheckedChange={(c) =>
                      setSelectedIds(
                        c === true ? new Set(rows.map((t) => t.id)) : new Set(),
                      )
                    }
                  />
                </Th>
                <Th stickyTop={112}>Date</Th>
                <Th stickyTop={112}>Merchant</Th>
                <Th stickyTop={112}>Tags</Th>
                <Th stickyTop={112}>Category</Th>
                <Th stickyTop={112}>Account</Th>
                <Th align="right" last stickyTop={112}>
                  Amount
                </Th>
              </tr>
            </thead>
            <tbody>
              {txnsQuery.status === "pending" ? (
                <StateRow colSpan={7}>Loading…</StateRow>
              ) : txnsQuery.status === "error" ? (
                <StateRow colSpan={7} tone="error">
                  Couldn’t load expenses — is the API running?
                </StateRow>
              ) : rows.length === 0 ? (
                <StateRow colSpan={7}>Nothing to show.</StateRow>
              ) : (
                rows.map((t, i) => {
                  const account = accountsById.get(t.account_id);
                  // Archived categories drop out of the active list, so the
                  // lookup can miss — `?? null` falls the dot back to derived.
                  const category =
                    t.category_id != null
                      ? categoriesById.get(t.category_id)
                      : undefined;
                  const categoryName = category?.name ?? "Uncategorized";
                  const merchant = t.merchant_raw?.trim() || "—";
                  return (
                    <TxnRow
                      key={t.id}
                      last={i === rows.length - 1}
                      date={t.date}
                      merchant={merchant}
                      labels={t.labels}
                      categoryId={t.category_id}
                      categoryName={categoryName}
                      categoryColor={category?.color ?? null}
                      accountName={account?.name ?? "—"}
                      accountLast4={account?.last4 ?? null}
                      amountPaise={t.amount_paise}
                      selected={selectedIds.has(t.id)}
                      onToggleSelect={() => toggleRow(t.id)}
                      onOpen={() => setSelectedTxn(t)}
                    />
                  );
                })
              )}
            </tbody>
          </table>
        </div>

        {/* Infinite-scroll sentinel: fetches the next page when it scrolls into
            view (no "Load more" button → no key-change reload, no scroll jump). */}
        {hasNextPage ? (
          <div ref={sentinelRef} className="border-t border-border/70">
            <p
              className="py-3 text-center text-[12px] text-muted-foreground"
              style={{ letterSpacing: "-0.003em" }}
            >
              {isFetchingNextPage ? "Loading…" : " "}
            </p>
          </div>
        ) : null}
      </section>

      <p className="mt-4 text-[12px] text-muted-foreground">
        {txnsQuery.status === "success" ? `Showing ${rows.length}` : " "}
      </p>
      {selectedTxn ? (
        <TransactionDialog
          key={selectedTxn.id}
          txn={selectedTxn}
          categories={categoriesQuery.data ?? []}
          accounts={accountsQuery.data ?? []}
          // Fallback label for a row whose account is ARCHIVED: GET /accounts
          // serves active accounts only, so it is absent from `accounts` and the
          // picker has no option to render as the current value.
          accountLabelText={accountLabelFor(
            accountsById.get(selectedTxn.account_id),
          )}
          onClose={() => setSelectedTxn(null)}
        />
      ) : null}

      {selectedLoadedIds.length > 0 ? (
        <SelectionBar
          ids={selectedLoadedIds}
          categories={categoriesQuery.data ?? []}
          categoryKind={typeFilter === "income" ? "income" : "spend"}
          onClear={() => setSelectedIds(new Set())}
          onDone={() => setSelectedIds(new Set())}
        />
      ) : null}
    </>
  );
}

function accountLabelFor(account: AccountRead | undefined): string {
  // "—" is the board's empty-state for a missing account; the label itself
  // comes from the shared helper.
  return account ? accountLabel(account) : "—";
}

function TxnRow({
  last,
  date,
  merchant,
  labels,
  categoryId,
  categoryName,
  categoryColor,
  accountName,
  accountLast4,
  amountPaise,
  selected,
  onToggleSelect,
  onOpen,
}: {
  last: boolean;
  date: string;
  merchant: string;
  labels: LabelRead[];
  categoryId: number | null;
  categoryName: string;
  categoryColor: CategoryColor | null;
  accountName: string;
  accountLast4: string | null;
  amountPaise: number;
  selected: boolean;
  onToggleSelect: () => void;
  onOpen: () => void;
}) {
  // PRD §F4a: sign is the source of truth. Positive = credit (refund),
  // rendered green with a leading "+"; negative = spend, shown as a magnitude.
  const isCredit = amountPaise > 0;
  const border = last ? "" : "border-b border-border/70";

  return (
    <tr
      className="group cursor-pointer transition-colors duration-100 hover:bg-muted/50"
      onClick={onOpen}
    >
      <Td first borderClass={border}>
        {/* Stop the click bubbling so toggling selection doesn't also open the
            row dialog. */}
        <span
          onClick={(e) => e.stopPropagation()}
          onKeyDown={(e) => e.stopPropagation()}
        >
          <Checkbox
            aria-label={`Select ${merchant}`}
            checked={selected}
            onCheckedChange={() => onToggleSelect()}
          />
        </span>
      </Td>

      <Td borderClass={border}>
        <span
          className="text-[12.5px] tabular-nums text-foreground/80"
          style={{ fontVariantNumeric: "tabular-nums lining-nums" }}
        >
          {formatDate(date)}
        </span>
      </Td>

      <Td borderClass={border}>
        <div className="flex min-w-0 flex-col justify-center gap-0.5 min-h-[32px]">
          <button
            type="button"
            // Row-level onClick keeps whole-row mouse activation; this button is
            // the keyboard/AT-accessible opener (a <tr> must not be a button, and
            // cells must not nest inside one).
            onClick={(e) => {
              e.stopPropagation();
              onOpen();
            }}
            aria-label={`Edit ${merchant}`}
            title={merchant === "—" ? undefined : merchant}
            className="truncate rounded-sm text-left text-[13px] font-medium text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            style={{ letterSpacing: "-0.005em" }}
          >
            {merchant}
          </button>
        </div>
      </Td>

      <Td borderClass={border}>
        {/* Read-only tag chips. The row opens the dialog to edit (same contract
            as the category dot), so no remove `x` here. Fixed 200px column: show
            the first two, roll the rest into a muted +N. */}
        {labels.length > 0 ? (
          <div className="flex items-center gap-1 overflow-hidden">
            {labels.slice(0, 2).map((l) => (
              <LabelChip key={l.id} name={l.name} />
            ))}
            {labels.length > 2 ? (
              <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground/70">
                +{labels.length - 2}
              </span>
            ) : null}
          </div>
        ) : (
          <span className="text-[12px] text-muted-foreground/50">—</span>
        )}
      </Td>

      <Td borderClass={border}>
        <div className="flex min-w-0 items-center gap-2">
          <CategoryDot categoryId={categoryId} color={categoryColor} />
          <span
            className="truncate text-[12.5px] text-foreground/80"
            style={{ letterSpacing: "-0.003em" }}
          >
            {categoryName}
          </span>
        </div>
      </Td>

      <Td borderClass={border}>
        <div className="flex min-w-0 items-center gap-1.5">
          <span className="text-[12px] text-foreground/80">{accountName}</span>
          {accountLast4 ? (
            <span className="text-[11px] tabular-nums text-muted-foreground/70">
              {accountLast4}
            </span>
          ) : null}
        </div>
      </Td>

      <Td align="right" last borderClass={border}>
        <span
          className={cn(
            "text-[13px] font-medium tabular-nums",
            isCredit ? "text-pos" : "text-foreground",
          )}
          style={{
            fontFamily: "var(--font-jbmono), ui-monospace, monospace",
            fontVariantNumeric: "tabular-nums lining-nums",
            letterSpacing: "-0.012em",
          }}
        >
          <Sensitive>
            {isCredit ? "+" : ""}
            {formatINR(Math.abs(amountPaise))}
          </Sensitive>
        </span>
      </Td>
    </tr>
  );
}
