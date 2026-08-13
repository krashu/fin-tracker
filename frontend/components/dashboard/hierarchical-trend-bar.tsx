"use client";

import { useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useQuery } from "@tanstack/react-query";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Pill } from "@/components/ui/pill";
import { CategoryDot } from "@/components/category-dot";
import { Sensitive, useBalanceHidden } from "@/components/balance-visibility";
import { IconChevronDown } from "@/components/icons";
import {
  getHierarchicalTrend,
  type HierarchicalParentRef,
} from "@/lib/api/client";
import { compactINR, formatINR } from "@/lib/format";
import { periodLabel } from "@/lib/charts";
import { periodRange } from "@/lib/period";
import { deriveSubcategoryColor } from "@/lib/categories";
import { cn } from "@/lib/utils";

interface HierarchicalTrendBarProps {
  year: number;
  labelId?: number;
}

export function HierarchicalTrendBar({
  year,
  labelId,
}: HierarchicalTrendBarProps) {
  const { hidden } = useBalanceHidden();
  const [selectedParentId, setSelectedParentId] = useState<number | "all">("all");

  const { start, end } = periodRange({ year });
  const query = useQuery({
    queryKey: [
      "dashboards",
      "hierarchical-trend",
      { bucket: "month", start, end, label_id: labelId },
    ],
    queryFn: () =>
      getHierarchicalTrend({
        bucket: "month",
        start,
        end,
        label_id: labelId,
      }),
  });

  const parents = query.data?.parents ?? [];
  const buckets = query.data?.buckets ?? [];

  const selectedParentRef = useMemo(
    () =>
      selectedParentId !== "all"
        ? parents.find((p) => p.parent_id === selectedParentId) ?? null
        : null,
    [selectedParentId, parents],
  );

  // Build series definitions (keys, names, colors)
  const seriesConfig = useMemo(() => {
    if (selectedParentRef) {
      // Selected specific parent: subcategories are the stacked series
      return selectedParentRef.subcategories.map((sub, idx) => ({
        key: `sub_${sub.category_id ?? "uncat"}`,
        name: sub.category_name ?? "General",
        color: deriveSubcategoryColor(
          selectedParentRef.color,
          idx,
          selectedParentRef.subcategories.length,
        ),
        categoryId: sub.category_id,
      }));
    }

    // All parents view: parent categories are the stacked series
    return parents.map((p) => ({
      key: `parent_${p.parent_id ?? "uncat"}`,
      name: p.parent_name,
      color: p.color ?? "var(--muted-foreground)",
      parentId: p.parent_id,
    }));
  }, [selectedParentRef, parents]);

  // Build chart row data for Recharts
  const chartData = useMemo(() => {
    return buckets.map((bucket) => {
      const row: Record<string, string | number> = {
        period: periodLabel(bucket.period, "month"),
      };

      if (selectedParentRef) {
        // Find totals for the selected parent in this bucket
        const parentTotal = bucket.totals.find(
          (t) => t.parent_id === selectedParentRef.parent_id,
        );
        for (const sub of selectedParentRef.subcategories) {
          const subTot = parentTotal?.subcategories.find(
            (s) => s.category_id === sub.category_id,
          );
          // Signed magnitude: -total_paise (so spend > 0)
          const key = `sub_${sub.category_id ?? "uncat"}`;
          row[key] = subTot ? Math.max(0, -subTot.total_paise) : 0;
        }
      } else {
        // All parents view
        for (const p of parents) {
          const parentTotal = bucket.totals.find(
            (t) => t.parent_id === p.parent_id,
          );
          const key = `parent_${p.parent_id ?? "uncat"}`;
          row[key] = parentTotal ? Math.max(0, -parentTotal.total_paise) : 0;
        }
      }

      return row;
    });
  }, [buckets, selectedParentRef, parents]);

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between border-b pb-3">
        <div className="flex items-center gap-2">
          <CardTitle as="h2" className="text-[14px]">
            Hierarchical Spend Trend
          </CardTitle>
          <span className="text-[12.5px] text-muted-foreground">· {year}</span>
        </div>

        {parents.length > 0 && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Pill active>
                {selectedParentRef ? (
                  <>
                    <CategoryDot
                      categoryId={selectedParentRef.parent_id}
                      color={selectedParentRef.color}
                    />
                    <span className="truncate max-w-[150px]">
                      {selectedParentRef.parent_name}
                    </span>
                  </>
                ) : (
                  <span>All Parent Categories</span>
                )}
                <IconChevronDown className="size-3 opacity-70" />
              </Pill>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="max-h-72 w-56">
              <DropdownMenuItem onSelect={() => setSelectedParentId("all")}>
                <span className="font-medium">All Parent Categories</span>
              </DropdownMenuItem>
              {parents.map((p) => (
                <DropdownMenuItem
                  key={p.parent_id ?? "uncat"}
                  onSelect={() => setSelectedParentId(p.parent_id ?? "all")}
                  className="flex items-center gap-2"
                >
                  <CategoryDot categoryId={p.parent_id} color={p.color} />
                  <span className="truncate">{p.parent_name}</span>
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </CardHeader>

      <CardContent className="pt-4">
        {query.isError ? (
          <p className="py-8 text-center text-[13px] text-neg">
            Couldn’t load trend — is the API running?
          </p>
        ) : query.isSuccess && parents.length === 0 ? (
          <p className="py-8 text-center text-[13px] text-muted-foreground">
            No spending recorded for this period.
          </p>
        ) : (
          <div
            className={cn(
              "h-[280px] w-full",
              hidden && "pointer-events-none select-none blur-sm",
            )}
          >
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={chartData}
                margin={{ left: 4, right: 8, top: 8, bottom: 4 }}
              >
                <CartesianGrid vertical={false} stroke="var(--border)" opacity={0.6} />
                <XAxis
                  dataKey="period"
                  tickLine={false}
                  axisLine={false}
                  tickMargin={8}
                  className="text-[11px] fill-muted-foreground"
                />
                <YAxis
                  tickLine={false}
                  axisLine={false}
                  width={48}
                  tickFormatter={(v: number) => compactINR(v)}
                  className="text-[11px] fill-muted-foreground"
                />
                <Tooltip
                  formatter={(value: any, name: any) => [
                    formatINR(Number(value || 0)),
                    name,
                  ]}
                  contentStyle={{
                    backgroundColor: "var(--background)",
                    borderColor: "var(--border)",
                    borderRadius: "6px",
                    fontSize: "12px",
                  }}
                />
                <ReferenceLine y={0} stroke="var(--border)" strokeWidth={1} />
                {seriesConfig.map((s) => (
                  <Bar
                    key={s.key}
                    dataKey={s.key}
                    name={s.name}
                    fill={s.color}
                    stackId="spend_stack"
                    radius={[2, 2, 0, 0]}
                  />
                ))}
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
