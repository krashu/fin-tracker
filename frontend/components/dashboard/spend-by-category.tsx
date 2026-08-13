"use client";

/**
 * Monthly spend by category (PRD §F8 view 2). A ranked horizontal bar list for
 * the selected month from `GET /dashboards/spend-by-category`.
 *
 * `total_paise` is signed (spend negative, refund positive); rows arrive
 * server-ordered (most-negative first, uncategorized pinned last) and are
 * rendered AS RECEIVED — never client-re-sorted. Net-credit categories (refunds
 * outweigh spend in the window → positive total) are excluded from the spend
 * bars and the percentage base and surfaced separately, so a rare net-refunded
 * category can't silently inflate every other slice. Percentages are computed in
 * integer paise; ₹ formatting happens only at display.
 *
 * The month is /spending's SHARED anchor (UX-19) — this stepper moves the other two
 * monthly cards with it, while the trend cards' grain toggles stay independent. The
 * query key includes the month so stepping refetches (PRD §F9 — mounted query
 * won't refetch on staleTime alone).
 */
import { useMemo, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";

import { Card, CardContent, CardHeader } from "@/components/ui/card";
import {
  listCategories,
  listSpendByCategory,
  type CategoryColor,
} from "@/lib/api/client";
import { formatINR, formatMonthYear, formatPercent } from "@/lib/format";
import {
  categoryColorVar,
  categoryDisplayName,
  getParentSubcategorySpend,
  resolveCategoryColor,
  rollUpSpendByCategory,
} from "@/lib/categories";
import { cn } from "@/lib/utils";
import { CategoryDot } from "@/components/category-dot";
import { Sensitive, useBalanceHidden } from "@/components/balance-visibility";
import { IconArrowLeft, IconChevronRight } from "@/components/icons";

export function SpendByCategory({
  labelId,
  year,
  month,
}: {
  labelId?: number;
  year: number;
  month?: string;
}) {
  const { hidden } = useBalanceHidden();
  const [viewMode, setViewMode] = useState<"parent" | "flat">("parent");
  const [selectedParentId, setSelectedParentId] = useState<number | null>(null);

  const query = useQuery({
    queryKey: month
      ? ["dashboards", "spend-by-category", { month, label_id: labelId }]
      : ["dashboards", "spend-by-category", { year: String(year), label_id: labelId }],
    queryFn: () =>
      month
        ? listSpendByCategory({ month, label_id: labelId })
        : listSpendByCategory({ year: String(year), label_id: labelId }),
  });
  const rows = query.data?.rows ?? [];

  // Previous period (month or year) for the per-category delta chip.
  const prevParams = useMemo(() => {
    if (month) {
      const [y, m] = month.split("-").map(Number);
      const d = new Date(y, m - 2, 1);
      const prevM = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
      return { month: prevM };
    }
    return { year: String(year - 1) };
  }, [month, year]);

  const prevQuery = useQuery({
    queryKey: ["dashboards", "spend-by-category", { ...prevParams, label_id: labelId }],
    queryFn: () => listSpendByCategory({ ...prevParams, label_id: labelId }),
    enabled: month != null,
  });

  // The aggregate carries only id/name/total — join the shared ["categories"] cache.
  const categoriesQuery = useQuery({
    queryKey: ["categories"],
    queryFn: listCategories,
  });
  const allCategories = categoriesQuery.data ?? [];
  const colorById = useMemo(
    () =>
      new Map<number, CategoryColor | null>(
        allCategories.map((c) => [c.id, resolveCategoryColor(c, allCategories)]),
      ),
    [allCategories],
  );
  const colorFor = (id: number | null): CategoryColor | null =>
    id != null ? (colorById.get(id) ?? null) : null;

  // Split by sign. Spend rows drive the bars + percentage base; net-credit rows
  // (positive total) are listed apart; exact-zero rows are dropped (no signal).
  const spendRows = useMemo(() => rows.filter((r) => r.total_paise < 0), [rows]);
  const creditRows = useMemo(() => rows.filter((r) => r.total_paise > 0), [rows]);
  const totalSpend = useMemo(
    () => spendRows.reduce((sum, r) => sum - r.total_paise, 0),
    [spendRows],
  );

  // Hierarchical rollups for current and previous period
  const parentRollups = useMemo(
    () => rollUpSpendByCategory(rows, allCategories),
    [rows, allCategories],
  );
  const prevRollups = useMemo(
    () => rollUpSpendByCategory(prevQuery.data?.rows ?? [], allCategories),
    [prevQuery.data?.rows, allCategories],
  );

  const prevParentMagById = useMemo(() => {
    const map = new Map<number | null, number>();
    for (const r of prevRollups) {
      if (r.totalPaise < 0) map.set(r.parentId, -r.totalPaise);
    }
    return map;
  }, [prevRollups]);

  const prevSubcatMagById = useMemo(() => {
    const map = new Map<number | null, number>();
    for (const r of prevQuery.data?.rows ?? []) {
      if (r.total_paise < 0) map.set(r.category_id, -r.total_paise);
    }
    return map;
  }, [prevQuery.data?.rows]);

  const spendRollups = useMemo(
    () => parentRollups.filter((r) => r.totalPaise < 0),
    [parentRollups],
  );
  const creditRollups = useMemo(
    () => parentRollups.filter((r) => r.totalPaise > 0),
    [parentRollups],
  );

  // Selected parent category details when drilled down
  const selectedRollup = useMemo(
    () =>
      selectedParentId != null
        ? parentRollups.find((r) => r.parentId === selectedParentId) ?? null
        : null,
    [selectedParentId, parentRollups],
  );

  const subcategorySpendItems = useMemo(() => {
    if (selectedParentId == null) return [];
    return getParentSubcategorySpend(spendRows, selectedParentId, allCategories);
  }, [selectedParentId, spendRows, allCategories]);

  const displayPeriodLabel = month
    ? formatMonthYear(new Date(Number(month.split("-")[0]), Number(month.split("-")[1]) - 1, 1))
    : `${year}`;

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between border-b gap-2 flex-wrap">
        <div className="flex items-center gap-2">
          {selectedParentId != null ? (
            <div className="flex items-center gap-1.5">
              <button
                type="button"
                onClick={() => setSelectedParentId(null)}
                className="flex items-center gap-1 text-[13px] font-medium text-muted-foreground hover:text-foreground transition-colors px-1.5 py-0.5 -ml-1.5 rounded hover:bg-muted"
                title="Back to parent categories"
              >
                <IconArrowLeft className="size-3.5" />
                <span>All categories</span>
              </button>
              <span className="text-muted-foreground text-[12px]">/</span>
              <span className="text-[14px] font-semibold text-foreground">
                {selectedRollup?.parentName ?? "Category"}
              </span>
              <span className="text-[12.5px] text-muted-foreground">· {displayPeriodLabel}</span>
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <span className="text-[14px] font-semibold text-foreground">
                Spend by category
              </span>
              <span className="text-[12.5px] text-muted-foreground">· {displayPeriodLabel}</span>
            </div>
          )}
        </div>

        <div className="flex items-center gap-3">
          {/* View mode toggle (only when not drilled down) */}
          {selectedParentId == null && spendRows.length > 0 ? (
            <div className="flex items-center rounded-lg border border-border bg-muted/40 p-0.5 text-[11.5px]">
              <button
                type="button"
                onClick={() => setViewMode("parent")}
                className={cn(
                  "px-2 py-0.5 rounded-md font-medium transition-colors",
                  viewMode === "parent"
                    ? "bg-background text-foreground shadow-xs"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                By Parent
              </button>
              <button
                type="button"
                onClick={() => setViewMode("flat")}
                className={cn(
                  "px-2 py-0.5 rounded-md font-medium transition-colors",
                  viewMode === "flat"
                    ? "bg-background text-foreground shadow-xs"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                All Detailed
              </button>
            </div>
          ) : null}

          <span
            className="text-[12.5px] font-medium tabular-nums text-muted-foreground"
            style={{ fontVariantNumeric: "tabular-nums lining-nums" }}
          >
            {query.isSuccess ? (
              selectedParentId != null && selectedRollup ? (
                <>
                  <Sensitive>{formatINR(-selectedRollup.totalPaise)}</Sensitive> in category
                </>
              ) : (
                <>
                  <Sensitive>{formatINR(totalSpend)}</Sensitive> spent
                </>
              )
            ) : (
              " "
            )}
          </span>
        </div>
      </CardHeader>

      <CardContent className="py-3">
        {query.isPending ? (
          <Empty>Loading…</Empty>
        ) : query.isError ? (
          <Empty tone="error">Couldn’t load — is the API running?</Empty>
        ) : spendRows.length === 0 ? (
          <Empty>No spending in {displayPeriodLabel}.</Empty>
        ) : selectedParentId != null ? (
          /* =================== DRILLDOWN VIEW =================== */
          <div className="flex flex-col gap-3">
            <div className="flex items-center justify-between text-[12px] text-muted-foreground pb-1">
              <span>
                Subcategories breakdown for{" "}
                <strong className="text-foreground font-medium">
                  {selectedRollup?.parentName}
                </strong>
              </span>
              {selectedParentId != null ? (
                <Link
                  href={`/expenses?category_id=${selectedParentId}`}
                  className="text-[11.5px] text-primary hover:underline"
                >
                  View all transactions →
                </Link>
              ) : null}
            </div>

            <ul className="flex flex-col gap-2.5">
              {subcategorySpendItems.map((subcat) => {
                const mag = -subcat.totalPaise;
                const parentTotalMag = selectedRollup ? -selectedRollup.totalPaise : 0;
                const shareOfParentPct =
                  parentTotalMag > 0 ? (mag * 100) / parentTotalMag : 0;

                return (
                  <li key={subcat.categoryId ?? "direct"}>
                    <div className="mb-1 flex items-baseline justify-between gap-2">
                      <Link
                        href={
                          subcat.categoryId != null
                            ? `/expenses?category_id=${subcat.categoryId}`
                            : `/expenses`
                        }
                        className={cn(
                          "group flex min-w-0 items-center gap-2 text-[12.5px] hover:underline",
                          subcat.isDirect
                            ? "text-muted-foreground italic"
                            : "text-foreground",
                        )}
                        title="Filter transactions by this category"
                      >
                        <CategoryDot
                          categoryId={subcat.categoryId}
                          color={colorFor(subcat.categoryId)}
                        />
                        <span className="truncate">{subcat.categoryName}</span>
                      </Link>
                      <span
                        className="flex shrink-0 items-baseline gap-1.5 text-[12px] tabular-nums text-muted-foreground"
                        style={{ fontVariantNumeric: "tabular-nums lining-nums" }}
                      >
                        <Sensitive>
                          <span className="text-foreground/80">{formatINR(mag)}</span> ·{" "}
                          {shareOfParentPct.toFixed(1)}%
                        </Sensitive>
                        {prevQuery.isSuccess && subcat.categoryId != null ? (
                          <DeltaChip
                            mag={mag}
                            prevMag={prevSubcatMagById.get(subcat.categoryId)}
                          />
                        ) : null}
                      </span>
                    </div>
                    <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                      <div
                        className="h-full rounded-full"
                        style={{
                          width: hidden ? "0%" : `${shareOfParentPct}%`,
                          backgroundColor: categoryColorVar(
                            subcat.categoryId,
                            colorFor(subcat.categoryId),
                          ),
                        }}
                      />
                    </div>
                  </li>
                );
              })}
            </ul>
          </div>
        ) : viewMode === "parent" ? (
          /* =================== PARENT ROLLUP VIEW =================== */
          <ul className="flex flex-col gap-2.5">
            {spendRollups.map((r) => {
              const mag = -r.totalPaise;
              const sharePct = totalSpend > 0 ? (mag * 100) / totalSpend : 0;
              const hasSubcategories = r.parentId != null && r.subcategories.length > 0;

              return (
                <li key={r.parentId ?? "uncategorized"}>
                  <div className="mb-1 flex items-baseline justify-between gap-2">
                    <div className="flex min-w-0 items-center gap-1.5">
                      {hasSubcategories ? (
                        <button
                          type="button"
                          onClick={() => setSelectedParentId(r.parentId)}
                          className={cn(
                            "group flex min-w-0 items-center gap-2 text-[12.5px] text-left hover:text-primary transition-colors",
                            r.parentId == null
                              ? "text-muted-foreground"
                              : "text-foreground font-medium",
                          )}
                          title="Click to drill down into subcategories"
                        >
                          <CategoryDot
                            categoryId={r.parentId}
                            color={colorFor(r.parentId)}
                          />
                          <span className="truncate">
                            {r.parentName ?? "Uncategorized"}
                          </span>
                          <span className="inline-flex items-center gap-0.5 rounded px-1.5 py-0.2 text-[10.5px] font-normal text-muted-foreground bg-muted group-hover:bg-primary/10 group-hover:text-primary">
                            {r.subcategories.length}{" "}
                            {r.subcategories.length === 1 ? "subcat" : "subcats"}
                            <IconChevronRight className="size-2.5 opacity-70" />
                          </span>
                        </button>
                      ) : (
                        <Link
                          href={
                            r.parentId != null
                              ? `/expenses?category_id=${r.parentId}`
                              : "/expenses"
                          }
                          className={cn(
                            "flex min-w-0 items-center gap-2 text-[12.5px] hover:underline",
                            r.parentId == null
                              ? "text-muted-foreground"
                              : "text-foreground",
                          )}
                          title="View transactions"
                        >
                          <CategoryDot
                            categoryId={r.parentId}
                            color={colorFor(r.parentId)}
                          />
                          <span className="truncate">
                            {r.parentName ?? "Uncategorized"}
                          </span>
                        </Link>
                      )}
                    </div>

                    <span
                      className="flex shrink-0 items-baseline gap-1.5 text-[12px] tabular-nums text-muted-foreground"
                      style={{ fontVariantNumeric: "tabular-nums lining-nums" }}
                    >
                      <Sensitive>
                        <span className="text-foreground/80">{formatINR(mag)}</span> ·{" "}
                        {sharePct.toFixed(1)}%
                      </Sensitive>
                      {prevQuery.isSuccess ? (
                        <DeltaChip
                          mag={mag}
                          prevMag={prevParentMagById.get(r.parentId)}
                        />
                      ) : null}
                    </span>
                  </div>
                  <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                    <div
                      className="h-full rounded-full"
                      style={{
                        width: hidden ? "0%" : `${sharePct}%`,
                        backgroundColor: categoryColorVar(
                          r.parentId,
                          colorFor(r.parentId),
                        ),
                      }}
                    />
                  </div>
                </li>
              );
            })}
          </ul>
        ) : (
          /* =================== FLAT DETAILED VIEW =================== */
          <ul className="flex flex-col gap-2.5">
            {spendRows.map((r) => {
              const mag = -r.total_paise;
              const sharePct = totalSpend > 0 ? (mag * 100) / totalSpend : 0;
              const catObj = allCategories.find((c) => c.id === r.category_id);
              const displayName = categoryDisplayName(catObj, allCategories);

              return (
                <li key={r.category_id ?? "uncategorized"}>
                  <div className="mb-1 flex items-baseline justify-between gap-2">
                    <Link
                      href={
                        r.category_id != null
                          ? `/expenses?category_id=${r.category_id}`
                          : "/expenses"
                      }
                      className={cn(
                        "flex min-w-0 items-center gap-2 text-[12.5px] hover:underline",
                        r.category_id == null
                          ? "text-muted-foreground"
                          : "text-foreground",
                      )}
                    >
                      <CategoryDot
                        categoryId={r.category_id}
                        color={colorFor(r.category_id)}
                      />
                      <span className="truncate">
                        {r.category_id == null ? "Uncategorized" : displayName}
                      </span>
                    </Link>
                    <span
                      className="flex shrink-0 items-baseline gap-1.5 text-[12px] tabular-nums text-muted-foreground"
                      style={{ fontVariantNumeric: "tabular-nums lining-nums" }}
                    >
                      <Sensitive>
                        <span className="text-foreground/80">{formatINR(mag)}</span> ·{" "}
                        {sharePct.toFixed(1)}%
                      </Sensitive>
                      {prevQuery.isSuccess ? (
                        <DeltaChip
                          mag={mag}
                          prevMag={prevSubcatMagById.get(r.category_id)}
                        />
                      ) : null}
                    </span>
                  </div>
                  <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                    <div
                      className="h-full rounded-full"
                      style={{
                        width: hidden ? "0%" : `${sharePct}%`,
                        backgroundColor: categoryColorVar(
                          r.category_id,
                          colorFor(r.category_id),
                        ),
                      }}
                    />
                  </div>
                </li>
              );
            })}
          </ul>
        )}

        {/* Name the percentage base (UX-20) */}
        {query.isSuccess && spendRows.length > 0 ? (
          <p className="mt-2.5 text-[11px] text-muted-foreground">
            {selectedParentId != null
              ? "% of category spend — refunds net into their category."
              : "% of net spend — refunds net into their category."}
          </p>
        ) : null}

        {creditRows.length > 0 && selectedParentId == null ? (
          <p className="mt-3 border-t border-border/60 pt-2.5 text-[11.5px] text-muted-foreground">
            Net credit this month:{" "}
            {(viewMode === "parent" ? creditRollups : creditRows).map((r, i) => {
              const catId = "parentId" in r ? r.parentId : r.category_id;
              const catName = "parentName" in r ? r.parentName : r.category_name;
              const total = "totalPaise" in r ? r.totalPaise : r.total_paise;
              return (
                <span key={catId ?? `uncategorized-${i}`}>
                  {i > 0 ? ", " : ""}
                  <CategoryDot
                    categoryId={catId}
                    color={colorFor(catId)}
                    className="mr-1 inline-block align-middle"
                  />
                  {catName ?? "Uncategorized"}{" "}
                  <span className="tabular-nums text-pos">
                    <Sensitive>+{formatINR(total)}</Sensitive>
                  </span>
                </span>
              );
            })}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

/** "vs last month" spend-change chip. `mag` / `prevMag` are positive magnitudes
 * (paise). Not wrapped in `Sensitive`: a percentage change is a ratio, not an
 * amount, so it leaks no balance (same stance as `formatPercent`). Color follows
 * spend semantics — more spending is "bad" (neg/red), less is "good" (pos/green),
 * the inverse of the money sign convention. */
function DeltaChip({
  mag,
  prevMag,
}: {
  mag: number;
  prevMag: number | undefined;
}) {
  // Absent last month (or last month was net-credit/zero) → nothing to compare.
  if (prevMag === undefined) {
    return <span className="text-[11px] text-muted-foreground">new</span>;
  }
  const pct = (mag - prevMag) / prevMag;
  // Round to 0.1% so sub-tick noise and "-0%" collapse to a flat marker.
  if (Math.round(pct * 1000) === 0) {
    return (
      <span className="text-[11px] tabular-nums text-muted-foreground">
        ±0%
      </span>
    );
  }
  const up = pct > 0; // spent more than last month
  return (
    <span
      className={cn("text-[11px] tabular-nums", up ? "text-neg" : "text-pos")}
      title="vs last month"
    >
      {up ? "▲" : "▼"} {formatPercent(Math.abs(pct))}
    </span>
  );
}

function Empty({
  children,
  tone,
}: {
  children: React.ReactNode;
  tone?: "error";
}) {
  return (
    <p
      className={cn(
        "py-8 text-center text-[13px]",
        tone === "error" ? "text-neg" : "text-muted-foreground",
      )}
    >
      {children}
    </p>
  );
}

