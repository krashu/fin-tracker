"use client";

/**
 * Category spend over time (PRD §F8 view 3 — "how is my category mix shifting?").
 * A single category at a time, picked from a dropdown, over the trailing 12
 * calendar months of `GET /dashboards/spend-by-category-by-period`. One category
 * per chart reads far better than a 9-way stack; the dropdown options come from
 * the response's `categories` set (only categories with activity in the window,
 * ordered biggest-spender first) and switching is instant — the one fetch
 * carries every category's series, so no refetch on selection.
 *
 * Each bar is the SIGNED magnitude (`-total_paise`): spend is a positive bar,
 * and a rare net-credit month (refunds > spend) dips below the `y=0` reference
 * line. A single series can render negative, so unlike a stack this view is
 * lossless — no flooring. Colored from the same shared `["categories"]` palette
 * as the spend-by-category card above it.
 *
 * Owns its own window + selection, independent of the other /spending cards.
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ReferenceLine,
  XAxis,
  YAxis,
} from "recharts";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Pill } from "@/components/ui/pill";
import { IconChevronDown } from "@/components/icons";
import { CategoryDot } from "@/components/category-dot";
import {
  listCategories,
  listSpendByCategoryByPeriod,
  type CategoryColor,
} from "@/lib/api/client";
import { compactINR, formatINR } from "@/lib/format";
import { periodLabel } from "@/lib/charts";
import { periodRange } from "@/lib/period";
import {
  categoryColorVar,
  categoryDisplayName,
  resolveCategoryColor,
} from "@/lib/categories";
import { cn } from "@/lib/utils";
import { useBalanceHidden } from "@/components/balance-visibility";

// The selected category is tracked by a stable string key: the uncategorized
// bucket's null id maps to a sentinel (a raw null makes a poor state/key). Same
// null-handling the spend-by-category card uses for its uncat row.
const UNCATEGORIZED_KEY = "uncategorized";
const keyFor = (id: number | null): string =>
  id == null ? UNCATEGORIZED_KEY : String(id);

export function CategoryTrendBar({ labelId, year }: { labelId?: number; year: number }) {
  const { hidden } = useBalanceHidden();
  const [selectedKey, setSelectedKey] = useState<string | null>(null);

  const { start, end } = periodRange({ year });
  const query = useQuery({
    queryKey: [
      "dashboards",
      "spend-by-category-by-period",
      { bucket: "month", start, end, label_id: labelId },
    ],
    queryFn: () =>
      listSpendByCategoryByPeriod({
        bucket: "month",
        start,
        end,
        label_id: labelId,
      }),
  });

  // The aggregate carries only id/name/total — join the shared ["categories"]
  // cache for the user-picked color (id → token). Until it resolves the lookup
  // misses and bars fall back to the muted default (a one-tick flip, acceptable).
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

  const cats = query.data?.categories ?? [];
  // Resolve the active category: the picked one, else the first (biggest spender),
  // else none. Defaulting here (not in state) keeps the default correct even
  // before the first render with data.
  const activeCat =
    cats.find((c) => keyFor(c.category_id) === selectedKey) ?? cats[0] ?? null;
  const activeKey = activeCat ? keyFor(activeCat.category_id) : null;
  const activeColor = categoryColorVar(
    activeCat?.category_id ?? null,
    activeCat?.category_id != null
      ? (colorById.get(activeCat.category_id) ?? null)
      : null,
  );

  const activeCatObj = allCategories.find((c) => c.id === activeCat?.category_id);
  const activeDisplayName = useMemo(() => {
    if (!activeCat) return "Uncategorized";
    if (activeCat.category_id == null) return "Uncategorized";
    return categoryDisplayName(activeCatObj, allCategories);
  }, [activeCat, activeCatObj, allCategories]);

  // Set of category IDs to aggregate for the active selection. If a parent category
  // is selected, this includes the parent ID and all its child subcategory IDs.
  const activeTargetCategoryIds = useMemo(() => {
    if (!activeCat || activeCat.category_id == null) return null;
    const catId = activeCat.category_id;
    const children = allCategories.filter((c) => c.parent_id === catId);
    if (children.length === 0) return new Set([catId]);
    return new Set([catId, ...children.map((c) => c.id)]);
  }, [activeCat, allCategories]);

  // One row per month for the selected category (with parent rollup support).
  // `value` is the signed magnitude (`-total_paise`): spend positive, net-credit
  // month negative (dips below y=0).
  const data = useMemo(() => {
    const buckets = query.data?.buckets ?? [];
    return buckets.map((b) => {
      if (activeTargetCategoryIds == null) {
        // Uncategorized
        const cell = b.totals.find((t) => t.category_id == null);
        return {
          label: periodLabel(b.period, "month"),
          value: cell ? -cell.total_paise : 0,
        };
      }

      // Sum all matching cells (parent + subcategories if parent selected)
      let totalPaise = 0;
      for (const t of b.totals) {
        if (t.category_id != null && activeTargetCategoryIds.has(t.category_id)) {
          totalPaise += t.total_paise;
        }
      }

      return {
        label: periodLabel(b.period, "month"),
        value: -totalPaise,
      };
    });
  }, [query.data, activeTargetCategoryIds]);

  const chartConfig = useMemo(
    () =>
      ({
        value: {
          label: activeDisplayName,
          color: activeColor,
        },
      }) satisfies ChartConfig,
    [activeDisplayName, activeColor],
  );

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between border-b">
        <div className="flex items-center gap-2">
          <CardTitle as="h2" className="text-[14px]">
            Category spend over time
          </CardTitle>
          <span className="text-[12.5px] text-muted-foreground">· {year}</span>
        </div>
        {cats.length > 0 ? (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Pill active>
                <CategoryDot
                  categoryId={activeCat?.category_id ?? null}
                  color={
                    activeCat?.category_id != null
                      ? (colorById.get(activeCat.category_id) ?? null)
                      : null
                  }
                />
                <span className="truncate max-w-[160px]">{activeDisplayName}</span>
                <IconChevronDown className="size-3 opacity-70" />
              </Pill>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="max-h-72 w-56">
              {cats.map((c) => {
                const cObj = allCategories.find((cat) => cat.id === c.category_id);
                const displayName =
                  c.category_id == null
                    ? "Uncategorized"
                    : categoryDisplayName(cObj, allCategories);

                return (
                  <DropdownMenuItem
                    key={keyFor(c.category_id)}
                    onSelect={() => setSelectedKey(keyFor(c.category_id))}
                    className="flex items-center gap-2"
                  >
                    <CategoryDot
                      categoryId={c.category_id}
                      color={
                        c.category_id != null
                          ? (colorById.get(c.category_id) ?? null)
                          : null
                      }
                    />
                    <span className="truncate">{displayName}</span>
                  </DropdownMenuItem>
                );
              })}
            </DropdownMenuContent>
          </DropdownMenu>
        ) : null}
      </CardHeader>

      <CardContent className="pt-4">
        {query.isError ? (
          <p className="py-8 text-center text-[13px] text-neg">
            Couldn’t load — is the API running?
          </p>
        ) : query.isSuccess && cats.length === 0 ? (
          <p className="py-8 text-center text-[13px] text-muted-foreground">
            No spending in the last 12 months.
          </p>
        ) : (
          <ChartContainer
            config={chartConfig}
            className={cn(
              "aspect-auto h-[260px] w-full",
              // Blur the bars + Y-axis amounts and disable the tooltip so no
              // magnitude leaks while balances are hidden.
              hidden && "pointer-events-none select-none blur-sm",
            )}
          >
            <BarChart data={data} margin={{ left: 4, right: 4, top: 4 }}>
              <CartesianGrid vertical={false} />
              <XAxis
                dataKey="label"
                tickLine={false}
                axisLine={false}
                tickMargin={8}
                minTickGap={8}
                className="text-[10px]"
              />
              <YAxis
                tickLine={false}
                axisLine={false}
                width={48}
                tickFormatter={(v: number) => compactINR(v)}
                className="text-[10px]"
              />
              {/* Break-even baseline: a net-credit month dips below it. */}
              <ReferenceLine y={0} stroke="var(--border)" strokeWidth={1} />
              <ChartTooltip
                content={
                  <ChartTooltipContent
                    formatter={(value) => formatINR(Number(value))}
                  />
                }
              />
              <Bar dataKey="value" fill="var(--color-value)" radius={3} />
            </BarChart>
          </ChartContainer>
        )}
      </CardContent>
    </Card>
  );
}
