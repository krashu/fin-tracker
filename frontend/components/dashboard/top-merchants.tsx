"use client";

/**
 * Top merchants for a month (PRD §F8 view 3 — "where is the money actually
 * going?"). A ranked horizontal bar list from `GET /dashboards/top-merchants`,
 * the biggest spenders first.
 *
 * `total_paise` is signed (spend negative, refund positive); rows arrive
 * server-ordered (most-negative first) and capped at `limit` (default 5), with
 * the no-merchant bucket already excluded. Rendered AS RECEIVED — never
 * client-re-sorted. Net-credit merchants (refunds outweigh spend in the window →
 * positive total) are split out of the spend bars and the percentage base and
 * surfaced in a footnote, so a rare net-refunded merchant can't sit in the
 * ranking as a "spend". `sharePct` is share-of-**shown-spend** (integer paise);
 * under truncation the bars sum to 100% of what's shown, not of the month — the
 * "top N of M" caption carries that caveat.
 *
 * The month is /spending's SHARED anchor (UX-19) — this stepper moves the other two
 * monthly cards with it. The query key includes the month so stepping refetches
 * (PRD §F9).
 */
import { useQuery } from "@tanstack/react-query";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { listTopMerchants } from "@/lib/api/client";
import { formatINR, formatMonthYear } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Sensitive, useBalanceHidden } from "@/components/balance-visibility";

export function TopMerchants({
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
      ? ["dashboards", "top-merchants", { month, label_id: labelId }]
      : ["dashboards", "top-merchants", { year: String(year), label_id: labelId }],
    queryFn: () =>
      month
        ? listTopMerchants({ month, label_id: labelId })
        : listTopMerchants({ year: String(year), label_id: labelId }),
  });
  const rows = query.data?.rows ?? [];
  const totalMerchants = query.data?.total_merchants ?? 0;
  const truncated = query.data?.truncated ?? false;

  // Split by sign. Spend rows (negative total) drive the bars + percentage base;
  // net-credit rows (positive total) are listed apart.
  const spendRows = rows.filter((r) => r.total_paise < 0);
  const creditRows = rows.filter((r) => r.total_paise > 0);
  const shownTotal = spendRows.reduce((sum, r) => sum - r.total_paise, 0);

  const displayPeriodLabel = month
    ? formatMonthYear(new Date(Number(month.split("-")[0]), Number(month.split("-")[1]) - 1, 1))
    : `${year}`;

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between border-b">
        <div className="flex items-center gap-2">
          <CardTitle as="h2" className="text-[14px]">
            Top merchants
          </CardTitle>
          <span className="text-[12.5px] text-muted-foreground">· {displayPeriodLabel}</span>
        </div>
      </CardHeader>

      <CardContent className="py-3">
        {query.isPending ? (
          <Empty>Loading…</Empty>
        ) : query.isError ? (
          <Empty tone="error">Couldn’t load — is the API running?</Empty>
        ) : spendRows.length === 0 ? (
          <Empty>No merchant spending in {displayPeriodLabel}.</Empty>
        ) : (
          <ul className="flex flex-col gap-2.5">
            {spendRows.map((r) => {
              const mag = -r.total_paise;
              // Integer-paise ratio; format only at the end (no float sums). The
              // bar width is driven by this same share-of-shown-total, so the bar
              // encodes exactly the % shown beside it.
              const sharePct = shownTotal > 0 ? (mag * 100) / shownTotal : 0;
              return (
                <li key={r.merchant_normalized}>
                  <div className="mb-1 flex items-baseline justify-between gap-2">
                    <span
                      className="min-w-0 truncate text-[12.5px] text-foreground"
                      title={r.merchant_label}
                    >
                      {r.merchant_label}
                    </span>
                    <span
                      className="shrink-0 text-[12px] tabular-nums text-muted-foreground"
                      style={{ fontVariantNumeric: "tabular-nums lining-nums" }}
                    >
                      <Sensitive>
                        <span className="text-foreground/80">
                          {formatINR(mag)}
                        </span>{" "}
                        · {sharePct.toFixed(1)}%
                      </Sensitive>
                    </span>
                  </div>
                  <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                    {/* width 0 when hidden so the bar leaks no magnitude (the
                        track stays for layout); amounts above are masked too. */}
                    <div
                      className="h-full rounded-full bg-primary"
                      style={{ width: hidden ? "0%" : `${sharePct}%` }}
                    />
                  </div>
                </li>
              );
            })}
          </ul>
        )}

        {creditRows.length > 0 ? (
          <p className="mt-3 border-t border-border/60 pt-2.5 text-[11.5px] text-muted-foreground">
            Net credit in {displayPeriodLabel}:{" "}
            {creditRows.map((r, i) => (
              <span key={r.merchant_normalized}>
                {i > 0 ? ", " : ""}
                {r.merchant_label}{" "}
                <span className="tabular-nums text-pos">
                  <Sensitive>+{formatINR(r.total_paise)}</Sensitive>
                </span>
              </span>
            ))}
          </p>
        ) : null}

        {/* The caption names the percentage base (UX-20). It differs from
            spend-by-category's on three counts — refunds are split out here as
            their own excluded credit rows instead of netting into a category, and
            this endpoint is LIMITed (default 5) while spend-by-category is not — so
            under truncation the base is the SHOWN rows, not the month's merchant
            spend. Calling it "gross spend" would be false, and truncated is the
            normal case. */}
        {query.isSuccess && totalMerchants > 0 ? (
          <p className="mt-2.5 text-[11px] text-muted-foreground">
            {truncated
              ? `Top ${rows.length} of ${totalMerchants} merchants · % of these ${rows.length}`
              : `${totalMerchants} ${totalMerchants === 1 ? "merchant" : "merchants"} · % of merchant spend`}
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
