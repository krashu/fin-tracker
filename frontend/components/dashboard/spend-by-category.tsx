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
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { IconChevronRight } from "@/components/icons";
import {
  listCategories,
  listSpendByCategory,
  type CategoryColor,
} from "@/lib/api/client";
import { formatINR, formatMonthYear, formatPercent } from "@/lib/format";
import { thisMonthAnchor, monthKey } from "@/lib/dates";
import { categoryColorVar } from "@/lib/categories";
import { cn } from "@/lib/utils";
import { CategoryDot } from "@/components/category-dot";
import { Sensitive, useBalanceHidden } from "@/components/balance-visibility";

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
  });

  const prevMagById = new Map<number | null, number>();
  for (const r of prevQuery.data?.rows ?? []) {
    if (r.total_paise < 0) prevMagById.set(r.category_id, -r.total_paise);
  }

  // The aggregate carries only id/name/total — join the shared ["categories"] cache.
  const categoriesQuery = useQuery({
    queryKey: ["categories"],
    queryFn: listCategories,
  });
  const colorById = new Map<number, CategoryColor | null>(
    (categoriesQuery.data ?? []).map((c) => [c.id, c.color]),
  );
  const colorFor = (id: number | null): CategoryColor | null =>
    id != null ? (colorById.get(id) ?? null) : null;

  // Split by sign. Spend rows drive the bars + percentage base; net-credit rows
  // (positive total) are listed apart; exact-zero rows are dropped (no signal).
  const spendRows = rows.filter((r) => r.total_paise < 0);
  const creditRows = rows.filter((r) => r.total_paise > 0);
  const totalSpend = spendRows.reduce((sum, r) => sum - r.total_paise, 0);

  const displayPeriodLabel = month
    ? formatMonthYear(new Date(Number(month.split("-")[0]), Number(month.split("-")[1]) - 1, 1))
    : `${year}`;

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between border-b">
        <div className="flex items-center gap-2">
          <span className="text-[14px] font-semibold text-foreground">
            Spend by category
          </span>
          <span className="text-[12.5px] text-muted-foreground">· {displayPeriodLabel}</span>
        </div>
        <span
          className="text-[12.5px] font-medium tabular-nums text-muted-foreground"
          style={{ fontVariantNumeric: "tabular-nums lining-nums" }}
        >
          {query.isSuccess ? (
            <>
              <Sensitive>{formatINR(totalSpend)}</Sensitive> spent
            </>
          ) : (
            " "
          )}
        </span>
      </CardHeader>

      <CardContent className="py-3">
        {query.isPending ? (
          <Empty>Loading…</Empty>
        ) : query.isError ? (
          <Empty tone="error">Couldn’t load — is the API running?</Empty>
        ) : spendRows.length === 0 ? (
          <Empty>No spending in {displayPeriodLabel}.</Empty>
        ) : (
          <ul className="flex flex-col gap-2.5">
            {spendRows.map((r) => {
              const mag = -r.total_paise;
              // Integer-paise ratio; format only at the end (no float sums). The
              // bar width below is driven by this same share-of-total, so the bar
              // encodes exactly the % shown beside it (not a max-relative scale).
              const sharePct = totalSpend > 0 ? (mag * 100) / totalSpend : 0;
              return (
                <li key={r.category_id ?? "uncategorized"}>
                  <div className="mb-1 flex items-baseline justify-between gap-2">
                    <span
                      className={cn(
                        "flex min-w-0 items-center gap-2 text-[12.5px]",
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
                        {r.category_name ?? "Uncategorized"}
                      </span>
                    </span>
                    <span
                      className="flex shrink-0 items-baseline gap-1.5 text-[12px] tabular-nums text-muted-foreground"
                      style={{ fontVariantNumeric: "tabular-nums lining-nums" }}
                    >
                      <Sensitive>
                        <span className="text-foreground/80">
                          {formatINR(mag)}
                        </span>{" "}
                        · {sharePct.toFixed(1)}%
                      </Sensitive>
                      {prevQuery.isSuccess ? (
                        <DeltaChip
                          mag={mag}
                          prevMag={prevMagById.get(r.category_id)}
                        />
                      ) : null}
                    </span>
                  </div>
                  <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                    {/* width 0 when hidden so the bar leaks no magnitude (the
                        track stays for layout); amounts above are masked too. */}
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

        {/* Name the percentage base (UX-20). It is NOT the same base as
            top-merchants': a refund nets into its own category here, whereas that
            card splits net-credit merchants out as excluded rows — and this endpoint
            has no LIMIT, so the base really is the whole month. */}
        {query.isSuccess && spendRows.length > 0 ? (
          <p className="mt-2.5 text-[11px] text-muted-foreground">
            % of net spend — refunds net into their category.
          </p>
        ) : null}

        {creditRows.length > 0 ? (
          <p className="mt-3 border-t border-border/60 pt-2.5 text-[11.5px] text-muted-foreground">
            Net credit this month:{" "}
            {creditRows.map((r, i) => (
              <span key={r.category_id ?? "uncategorized"}>
                {i > 0 ? ", " : ""}
                <CategoryDot
                  categoryId={r.category_id}
                  color={colorFor(r.category_id)}
                  className="mr-1 inline-block align-middle"
                />
                {r.category_name ?? "Uncategorized"}{" "}
                <span className="tabular-nums text-pos">
                  <Sensitive>+{formatINR(r.total_paise)}</Sensitive>
                </span>
              </span>
            ))}
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
