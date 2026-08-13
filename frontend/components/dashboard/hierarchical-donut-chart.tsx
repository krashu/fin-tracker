"use client";

import { useMemo, useState } from "react";
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Pill } from "@/components/ui/pill";
import { CategoryDot } from "@/components/category-dot";
import { Sensitive, useBalanceHidden } from "@/components/balance-visibility";
import { IconArrowLeft, IconChevronRight } from "@/components/icons";
import {
  getHierarchicalSpend,
  type HierarchicalParentSpend,
  type HierarchicalSubcategorySpend,
} from "@/lib/api/client";
import { formatINR, formatPercent } from "@/lib/format";
import { deriveSubcategoryColor } from "@/lib/categories";
import { cn } from "@/lib/utils";
import { useQuery } from "@tanstack/react-query";

interface HierarchicalDonutChartProps {
  month?: string;
  year: number;
  labelId?: number;
}

export function HierarchicalDonutChart({
  month,
  year,
  labelId,
}: HierarchicalDonutChartProps) {
  const { hidden } = useBalanceHidden();
  const [selectedParentId, setSelectedParentId] = useState<number | null>(null);
  const [hoveredSlice, setHoveredSlice] = useState<{
    name: string;
    amountPaise: number;
    percentage: number;
    parentName?: string;
    isSubcategory?: boolean;
    color?: string | null;
  } | null>(null);

  const query = useQuery({
    queryKey: month
      ? ["dashboards", "hierarchical-spend", { month, label_id: labelId }]
      : ["dashboards", "hierarchical-spend", { year: String(year), label_id: labelId }],
    queryFn: () =>
      month
        ? getHierarchicalSpend({ month, label_id: labelId })
        : getHierarchicalSpend({ year: String(year), label_id: labelId }),
  });

  const parents = query.data?.parents ?? [];
  const totalSpendPaise = query.data?.total_spend_paise ?? 0;

  // Filter parents with actual spend (spend_paise > 0)
  const activeParents = useMemo(
    () => parents.filter((p) => p.spend_paise > 0),
    [parents],
  );

  // Selected parent when drilled down
  const selectedParent = useMemo(
    () =>
      selectedParentId != null
        ? activeParents.find((p) => p.parent_id === selectedParentId) ?? null
        : null,
    [selectedParentId, activeParents],
  );

  // Inner Ring Data: Parent Categories
  const parentChartData = useMemo(() => {
    return activeParents.map((p) => ({
      id: p.parent_id,
      name: p.parent_name,
      value: p.spend_paise,
      percentage: p.percentage,
      color: p.color ?? "var(--muted-foreground)",
      raw: p,
    }));
  }, [activeParents]);

  // Outer Ring Data: All Subcategories grouped by parent or just selected parent
  const subcategoryChartData = useMemo(() => {
    if (selectedParent) {
      // Drilled down into one parent: show its subcategories
      const subs = selectedParent.subcategories.filter((s) => s.spend_paise > 0);
      return subs.map((s, idx) => ({
        id: s.category_id,
        name: s.category_name,
        value: s.spend_paise,
        percentage: s.percentage,
        parentName: selectedParent.parent_name,
        color:
          s.color ??
          deriveSubcategoryColor(selectedParent.color, idx, subs.length),
        raw: s,
      }));
    }

    // Full 2-ring view: all active subcategories across all parents
    const items: Array<{
      id: number | null;
      name: string;
      value: number;
      percentage: number;
      parentName: string;
      color: string;
      raw: HierarchicalSubcategorySpend;
    }> = [];

    for (const p of activeParents) {
      const activeSubs = p.subcategories.filter((s) => s.spend_paise > 0);
      activeSubs.forEach((s, idx) => {
        items.push({
          id: s.category_id,
          name: s.category_name,
          value: s.spend_paise,
          percentage: (s.spend_paise / totalSpendPaise) * 100,
          parentName: p.parent_name,
          color:
            s.color ??
            deriveSubcategoryColor(p.color, idx, activeSubs.length),
          raw: s,
        });
      });
    }

    return items;
  }, [activeParents, selectedParent, totalSpendPaise]);

  // Active Center Display Info
  const centerDisplay = useMemo(() => {
    if (hoveredSlice) {
      return {
        title: hoveredSlice.name,
        subtitle: hoveredSlice.parentName
          ? `${hoveredSlice.parentName} · ${formatPercent(hoveredSlice.percentage / 100)}`
          : `${formatPercent(hoveredSlice.percentage / 100)} of total`,
        amount: hoveredSlice.amountPaise,
        color: hoveredSlice.color,
      };
    }
    if (selectedParent) {
      return {
        title: selectedParent.parent_name,
        subtitle: `${formatPercent(selectedParent.percentage / 100)} of total spend`,
        amount: selectedParent.spend_paise,
        color: selectedParent.color,
      };
    }
    return {
      title: "Total Spend",
      subtitle: `${activeParents.length} parent categories`,
      amount: totalSpendPaise,
      color: undefined,
    };
  }, [hoveredSlice, selectedParent, activeParents.length, totalSpendPaise]);

  if (query.isLoading) {
    return (
      <Card>
        <CardHeader className="border-b">
          <CardTitle as="h2" className="text-[14px]">
            Hierarchical Spend Breakdown
          </CardTitle>
        </CardHeader>
        <CardContent className="h-[360px] flex items-center justify-center">
          <p className="text-[13px] text-muted-foreground animate-pulse">
            Loading spending taxonomy...
          </p>
        </CardContent>
      </Card>
    );
  }

  if (activeParents.length === 0) {
    return (
      <Card>
        <CardHeader className="border-b">
          <CardTitle as="h2" className="text-[14px]">
            Hierarchical Spend Breakdown
          </CardTitle>
        </CardHeader>
        <CardContent className="h-[280px] flex items-center justify-center">
          <p className="text-[13px] text-muted-foreground">
            No spending recorded for this period.
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="overflow-hidden">
      <CardHeader className="flex flex-row items-center justify-between border-b pb-3">
        <div className="flex items-center gap-2.5">
          <CardTitle as="h2" className="text-[14px]">
            Hierarchical Spend Breakdown
          </CardTitle>
          {selectedParent ? (
            <button
              onClick={() => {
                setSelectedParentId(null);
                setHoveredSlice(null);
              }}
              className="inline-flex items-center gap-1 text-[12px] font-medium text-primary hover:underline"
            >
              <IconArrowLeft className="size-3" />
              All Categories
            </button>
          ) : (
            <span className="text-[12px] text-muted-foreground">
              · Two-Ring Sunburst
            </span>
          )}
        </div>

        {selectedParent ? (
          <Pill active className="text-[11.5px]">
            <CategoryDot
              categoryId={selectedParent.parent_id}
              color={selectedParent.color}
            />
            {selectedParent.parent_name}
          </Pill>
        ) : (
          <span className="text-[11.5px] text-muted-foreground">
            Inner: Parent · Outer: Subcategory
          </span>
        )}
      </CardHeader>

      <CardContent className="p-6">
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-6 items-center">
          {/* Donut Chart with Center KPI */}
          <div className="lg:col-span-7 relative flex items-center justify-center h-[340px]">
            <div
              className={cn(
                "w-full h-full",
                hidden && "pointer-events-none select-none blur-sm",
              )}
            >
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  {/* Inner Ring: Parent Categories */}
                  {!selectedParent && (
                    <Pie
                      data={parentChartData}
                      dataKey="value"
                      nameKey="name"
                      cx="50%"
                      cy="50%"
                      innerRadius={68}
                      outerRadius={98}
                      paddingAngle={2}
                      onClick={(entry: any) => {
                        const payload = entry?.payload ?? entry;
                        if (payload?.id != null) {
                          setSelectedParentId(payload.id);
                          setHoveredSlice(null);
                        }
                      }}
                      onMouseEnter={(entry: any) => {
                        const payload = entry?.payload ?? entry;
                        setHoveredSlice({
                          name: payload?.name ?? "Category",
                          amountPaise: Number(payload?.value ?? 0),
                          percentage: Number(payload?.percentage ?? 0),
                          color: payload?.color,
                        });
                      }}
                      onMouseLeave={() => setHoveredSlice(null)}
                      cursor="pointer"
                    >
                      {parentChartData.map((entry, index) => (
                        <Cell
                          key={`parent-cell-${index}`}
                          fill={entry.color}
                          stroke="var(--background)"
                          strokeWidth={2}
                          className="transition-opacity hover:opacity-85"
                        />
                      ))}
                    </Pie>
                  )}

                  {/* Outer Ring: Subcategories */}
                  <Pie
                    data={subcategoryChartData}
                    dataKey="value"
                    nameKey="name"
                    cx="50%"
                    cy="50%"
                    innerRadius={selectedParent ? 75 : 105}
                    outerRadius={selectedParent ? 130 : 138}
                    paddingAngle={selectedParent ? 3 : 1}
                    onMouseEnter={(entry: any) => {
                      const payload = entry?.payload ?? entry;
                      setHoveredSlice({
                        name: payload?.name ?? "Subcategory",
                        amountPaise: Number(payload?.value ?? 0),
                        percentage: Number(payload?.percentage ?? 0),
                        parentName: payload?.parentName,
                        isSubcategory: true,
                        color: payload?.color,
                      });
                    }}
                    onMouseLeave={() => setHoveredSlice(null)}

                  >
                    {subcategoryChartData.map((entry, index) => (
                      <Cell
                        key={`subcat-cell-${index}`}
                        fill={entry.color}
                        stroke="var(--background)"
                        strokeWidth={2}
                        className="transition-opacity hover:opacity-85"
                      />
                    ))}
                  </Pie>

                  <Tooltip
                    content={() => null} // Center KPI card serves as the dynamic indicator
                  />
                </PieChart>
              </ResponsiveContainer>
            </div>

            {/* Center Dynamic KPI Card */}
            <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none text-center px-4">
              <span className="text-[12px] font-medium text-muted-foreground truncate max-w-[170px]">
                {centerDisplay.title}
              </span>
              <span className="text-[18px] font-semibold tracking-tight text-foreground my-0.5">
                <Sensitive>{formatINR(centerDisplay.amount)}</Sensitive>
              </span>
              <span className="text-[11px] text-muted-foreground/80">
                {centerDisplay.subtitle}
              </span>
            </div>
          </div>

          {/* Hierarchical Drilldown / Breakdown List */}
          <div className="lg:col-span-5 flex flex-col gap-2 max-h-[340px] overflow-y-auto pr-1">
            <div className="text-[12px] font-medium text-muted-foreground mb-1">
              {selectedParent
                ? `Subcategories of ${selectedParent.parent_name}`
                : "Top Parent Categories"}
            </div>

            {selectedParent ? (
              <div className="flex flex-col gap-1.5">
                {selectedParent.subcategories.map((sub, idx) => (
                  <div
                    key={sub.category_id ?? `sub-${idx}`}
                    className="flex items-center justify-between p-2 rounded-md hover:bg-muted/40 transition-colors text-[13px]"
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <CategoryDot
                        categoryId={sub.category_id}
                        color={
                          sub.color ??
                          deriveSubcategoryColor(
                            selectedParent.color,
                            idx,
                            selectedParent.subcategories.length,
                          )
                        }
                      />
                      <span className="truncate font-medium">
                        {sub.category_name}
                      </span>
                    </div>
                    <div className="flex items-center gap-2 text-right">
                      <span className="font-semibold text-[12.5px]">
                        <Sensitive>{formatINR(sub.spend_paise)}</Sensitive>
                      </span>
                      <span className="text-[11px] text-muted-foreground w-10">
                        {formatPercent(sub.percentage / 100)}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="flex flex-col gap-1.5">
                {activeParents.map((parent) => (
                  <button
                    key={parent.parent_id ?? "uncat"}
                    onClick={() => {
                      if (parent.parent_id != null) {
                        setSelectedParentId(parent.parent_id);
                      }
                    }}
                    className="flex items-center justify-between p-2 rounded-md hover:bg-muted/50 transition-colors text-[13px] text-left group"
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <CategoryDot
                        categoryId={parent.parent_id}
                        color={parent.color}
                      />
                      <div className="flex flex-col min-w-0">
                        <span className="truncate font-medium group-hover:text-primary transition-colors">
                          {parent.parent_name}
                        </span>
                        <span className="text-[10.5px] text-muted-foreground">
                          {parent.subcategories.length} subcategories
                        </span>
                      </div>
                    </div>
                    <div className="flex items-center gap-2 text-right">
                      <div className="flex flex-col items-end">
                        <span className="font-semibold text-[12.5px]">
                          <Sensitive>{formatINR(parent.spend_paise)}</Sensitive>
                        </span>
                        <span className="text-[10.5px] text-muted-foreground">
                          {formatPercent(parent.percentage / 100)}
                        </span>
                      </div>
                      <IconChevronRight className="size-3.5 text-muted-foreground opacity-40 group-hover:opacity-100 transition-opacity" />
                    </div>
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
