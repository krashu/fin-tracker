"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Pill } from "@/components/ui/pill";
import { Sensitive } from "@/components/balance-visibility";
import {
  getHierarchicalSpend,
  type SubcategoryMover,
} from "@/lib/api/client";
import { formatINR, formatPercent } from "@/lib/format";
import { cn } from "@/lib/utils";

interface SubcategoryMoversProps {
  month?: string;
  year: number;
  labelId?: number;
}

export function SubcategoryMovers({
  month,
  year,
  labelId,
}: SubcategoryMoversProps) {
  const query = useQuery({
    queryKey: month
      ? ["dashboards", "hierarchical-spend", { month, label_id: labelId }]
      : ["dashboards", "hierarchical-spend", { year: String(year), label_id: labelId }],
    queryFn: () =>
      month
        ? getHierarchicalSpend({ month, label_id: labelId })
        : getHierarchicalSpend({ year: String(year), label_id: labelId }),
  });

  const movers = query.data?.top_movers ?? [];

  const expanding = useMemo(
    () => movers.filter((m: SubcategoryMover) => m.delta_paise > 0).slice(0, 4),
    [movers],
  );
  const contracting = useMemo(
    () => movers.filter((m: SubcategoryMover) => m.delta_paise < 0).slice(0, 4),
    [movers],
  );

  if (query.isLoading || movers.length === 0) {
    return null;
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between border-b pb-3">
        <div className="flex items-center gap-2">
          <CardTitle as="h2" className="text-[14px]">
            Subcategory Spend Shifts
          </CardTitle>
          <span className="text-[12px] text-muted-foreground">
            · vs previous period
          </span>
        </div>
      </CardHeader>

      <CardContent className="p-4 grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* Fastest Growing Spends */}
        <div className="flex flex-col gap-2">
          <span className="text-[12px] font-medium text-muted-foreground flex items-center gap-1.5">
            <span className="size-1.5 rounded-full bg-neg" />
            Expanding Spend (MoM Increase)
          </span>
          {expanding.length === 0 ? (
            <p className="text-[12px] text-muted-foreground py-2">
              No significant spend increases.
            </p>
          ) : (
            <div className="flex flex-col gap-1.5">
              {expanding.map((m: SubcategoryMover) => (
                <MoverRow key={m.category_id ?? m.category_name} mover={m} />
              ))}
            </div>
          )}
        </div>

        {/* Fastest Contracting Spends */}
        <div className="flex flex-col gap-2">
          <span className="text-[12px] font-medium text-muted-foreground flex items-center gap-1.5">
            <span className="size-1.5 rounded-full bg-pos" />
            Contracting Spend (Savings / Reductions)
          </span>
          {contracting.length === 0 ? (
            <p className="text-[12px] text-muted-foreground py-2">
              No significant spend decreases.
            </p>
          ) : (
            <div className="flex flex-col gap-1.5">
              {contracting.map((m: SubcategoryMover) => (
                <MoverRow key={m.category_id ?? m.category_name} mover={m} />
              ))}
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}


function MoverRow({ mover }: { mover: SubcategoryMover }) {
  const isIncrease = mover.delta_paise > 0;
  const pctStr =
    mover.growth_rate != null
      ? `${mover.growth_rate > 0 ? "+" : ""}${mover.growth_rate.toFixed(0)}%`
      : "New";

  return (
    <div className="flex items-center justify-between p-2 rounded-md bg-muted/30 hover:bg-muted/50 transition-colors text-[12.5px]">
      <div className="flex flex-col min-w-0 pr-2">
        <span className="font-medium truncate">{mover.category_name}</span>
        {mover.parent_name && (
          <span className="text-[10.5px] text-muted-foreground truncate">
            {mover.parent_name}
          </span>
        )}
      </div>

      <div className="flex items-center gap-2.5 text-right shrink-0">
        <div className="flex flex-col items-end">
          <span className="font-semibold text-[12px]">
            <Sensitive>{formatINR(mover.current_paise)}</Sensitive>
          </span>
          <span
            className={cn(
              "text-[10.5px] font-medium",
              isIncrease ? "text-neg" : "text-pos",
            )}
          >
            {isIncrease ? "+" : ""}
            <Sensitive>{formatINR(mover.delta_paise)}</Sensitive>
          </span>
        </div>

        <span
          className={cn(
            "text-[11px] font-medium px-1.5 py-0.5 rounded",
            isIncrease
              ? "bg-neg/10 text-neg"
              : "bg-pos/10 text-pos",
          )}
        >
          {pctStr}
        </span>
      </div>
    </div>
  );
}
