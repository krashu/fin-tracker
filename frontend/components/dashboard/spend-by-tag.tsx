"use client";

/**
 * Monthly spend by tag + coverage guardrail (PRD §F3a labels; tag-analysis arc
 * Phase B). A ranked horizontal bar list for the selected month from
 * `GET /dashboards/spend-by-tag`.
 *
 * This is the tag analog of `SpendByCategory`, but two things differ because tags
 * are many:many (a txn can carry `#travel` + `#work`) where category is 1:1:
 *
 *  1. **Bars are MAX-RELATIVE, not share-of-total, and carry NO per-tag %**
 *     (arc decision #2 — a tag total is an independent number, "₹X on #travel",
 *     never "22% of spend"). `Σ(rows)` legitimately overshoots the month total
 *     (multi-tagged txns are counted under each tag), so a share-of-total would
 *     be a lie. The bar width encodes each tag's magnitude relative to the
 *     biggest bar, not to any total.
 *  2. **Coverage is the ONE percentage on the card** (arc decision #5 — the
 *     honesty guardrail, "% of spend that's tagged, by amount"). It IS a legit
 *     share-of-total because tagged + untagged partition the month's spend. When
 *     `coverage_rate` is null (no spend, or a refund-skewed month whose signed
 *     ratio escapes [0,1]) the header shows "—" and the coverage line is dropped.
 *
 * The untagged bucket (`label_id === null`) is the residual — pinned last and
 * muted, mirroring `SpendByCategory`'s uncategorized row. Net-credit tags
 * (refunds outweigh spend → positive total) are listed apart, never as a bar,
 * exactly like `SpendByCategory`.
 *
 * The month is /spending's SHARED anchor (UX-19) — this stepper moves the other two
 * monthly cards with it. The query key includes the month so stepping refetches, and
 * it shares the `["dashboards"]` invalidation prefix so it refreshes after an import
 * commit / edit. It is intentionally UNSCOPED by the /spending tag cross-filter —
 * it *is* the tag breakdown, so filtering it to one tag would collapse it to a
 * single bar.
 */
import { useQuery } from "@tanstack/react-query";

import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { listSpendByTag } from "@/lib/api/client";
import { formatINR, formatMonthYear } from "@/lib/format";
import { labelDisplay } from "@/lib/labels";
import { cn } from "@/lib/utils";
import { Sensitive, useBalanceHidden } from "@/components/balance-visibility";

export function SpendByTag({
  year,
  month,
}: {
  year: number;
  month?: string;
}) {
  const { hidden } = useBalanceHidden();

  const query = useQuery({
    queryKey: month
      ? ["dashboards", "spend-by-tag", { month }]
      : ["dashboards", "spend-by-tag", { year: String(year) }],
    queryFn: () =>
      month
        ? listSpendByTag({ month })
        : listSpendByTag({ year: String(year) }),
  });
  const data = query.data;
  const rows = data?.rows ?? [];

  // Split by sign, exactly like SpendByCategory: net-spend rows (total < 0) drive
  // the bars; net-credit rows (total > 0) are listed apart; exact-zero rows drop.
  const spendRows = rows.filter((r) => r.total_paise < 0);
  const creditRows = rows.filter((r) => r.total_paise > 0);
  // Max-relative scale (arc decision #2 — NOT share-of-total). Guard /0.
  const maxMag = spendRows.reduce((m, r) => Math.max(m, -r.total_paise), 0);

  // Coverage — the one legit percentage. Null (refund-skew / no spend) → "—".
  const rate = data?.coverage_rate ?? null;
  const coveragePct = rate == null ? null : Math.round(rate * 100);
  // When rate is in [0,1] the signed figures are guaranteed same-sign net-spend,
  // so these magnitudes are clean (tagged ≤ total). Only rendered in that case.
  const taggedMag = data ? -data.tagged_paise : 0;
  const totalMag = data ? -data.total_spend_paise : 0;
  const lowCoverage = rate != null && rate < 0.5;

  const displayPeriodLabel = month
    ? formatMonthYear(new Date(Number(month.split("-")[0]), Number(month.split("-")[1]) - 1, 1))
    : `${year}`;

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between border-b">
        <div className="flex items-center gap-2">
          <span className="text-[14px] font-semibold text-foreground">
            Spend by tag
          </span>
          <span className="text-[12.5px] text-muted-foreground">· {displayPeriodLabel}</span>
        </div>
        {/* Coverage % — a ratio, not an amount, so not masked (cf. formatPercent
            stance). "—" when null (no spend / refund-skew). */}
        <span className="flex flex-col items-end">
          <span
            className={cn(
              "text-[15px] font-semibold tabular-nums",
              coveragePct == null
                ? "text-muted-foreground"
                : lowCoverage
                  ? "text-[color:var(--warn)]"
                  : "text-foreground",
            )}
            style={{ fontVariantNumeric: "tabular-nums lining-nums" }}
          >
            {query.isSuccess
              ? coveragePct == null
                ? "—"
                : `${coveragePct}%`
              : " "}
          </span>
          {query.isSuccess ? (
            <span className="text-[11px] text-muted-foreground">tagged</span>
          ) : null}
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
              // Max-relative width (arc decision #2): biggest bar = 100%. NO % is
              // shown beside the amount — a tag total is not a share of anything.
              const widthPct = maxMag > 0 ? (mag * 100) / maxMag : 0;
              const isUntagged = r.label_id == null;
              return (
                <li key={r.label_id ?? "untagged"}>
                  <div className="mb-1 flex items-baseline justify-between gap-2">
                    <span
                      className={cn(
                        "min-w-0 truncate text-[12.5px]",
                        isUntagged
                          ? "text-muted-foreground"
                          : "text-foreground",
                      )}
                    >
                      {isUntagged
                        ? "Untagged"
                        : labelDisplay(r.label_name ?? "")}
                    </span>
                    <span
                      className="shrink-0 text-[12px] tabular-nums text-foreground/80"
                      style={{ fontVariantNumeric: "tabular-nums lining-nums" }}
                    >
                      <Sensitive>{formatINR(mag)}</Sensitive>
                    </span>
                  </div>
                  <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                    {/* width 0 when hidden so the bar leaks no magnitude. Untagged
                        gets a muted fill; tags get the accent. */}
                    <div
                      className={cn(
                        "h-full rounded-full",
                        isUntagged ? "bg-muted-foreground/40" : "bg-primary",
                      )}
                      style={{ width: hidden ? "0%" : `${widthPct}%` }}
                    />
                  </div>
                </li>
              );
            })}
          </ul>
        )}

        {/* Coverage line — the honesty guardrail (arc decision #5). Only shown
            when the rate is meaningful (then taggedMag ≤ totalMag are clean). */}
        {query.isSuccess && coveragePct != null ? (
          <p className="mt-3 text-[11.5px] text-muted-foreground">
            <Sensitive>
              {formatINR(taggedMag)} of {formatINR(totalMag)}
            </Sensitive>{" "}
            spend tagged
            {lowCoverage ? (
              <span className="text-[color:var(--warn)]">
                {" "}
                — most of your spend isn’t tagged yet
              </span>
            ) : null}
          </p>
        ) : null}

        {/* The many:many caveat (arc decisions #1 / #2): per-tag totals overlap,
            so they don't add up to the month total — bars only, never a pie. */}
        {query.isSuccess && spendRows.length > 0 ? (
          <p className="mt-2 text-[11px] text-muted-foreground">
            Tags overlap — these don’t sum to your total.
          </p>
        ) : null}

        {creditRows.length > 0 ? (
          <p className="mt-3 border-t border-border/60 pt-2.5 text-[11.5px] text-muted-foreground">
            Net credit this month:{" "}
            {creditRows.map((r, i) => (
              <span key={r.label_id ?? "untagged"}>
                {i > 0 ? ", " : ""}
                {r.label_id == null
                  ? "Untagged"
                  : labelDisplay(r.label_name ?? "")}{" "}
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
