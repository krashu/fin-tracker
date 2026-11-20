"use client";

/**
 * Client island for /holdings (Phase 2).
 *
 * Coordinates broker filtering via AccountFilterBar, re-aggregates KPI summary
 * cards, and passes scoped data to HoldingsTable with reactive URL search param
 * synchronization (?account_id=...).
 */

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";

import { listHoldings } from "@/lib/api/client";
import { getInvestmentAccounts } from "@/lib/api/investment-accounts";
import { AccountFilterBar } from "@/components/investments/account-filter-bar";
import { HoldingsSummaryCards } from "@/components/investments/holdings-summary-cards";
import { HoldingsTable } from "./holdings-table";

export function HoldingsView() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();

  const accountIdParam = searchParams.get("account_id");
  const parsedId = Number(accountIdParam);
  const accountId =
    accountIdParam && Number.isInteger(parsedId) && parsedId > 0
      ? parsedId
      : null;

  const handleSelectAccount = (id: number | null) => {
    const params = new URLSearchParams(searchParams.toString());
    if (id != null) {
      params.set("account_id", String(id));
    } else {
      params.delete("account_id");
    }
    const qs = params.toString();
    router.replace(`${pathname}${qs ? `?${qs}` : ""}`, { scroll: false });
  };

  const accountsQuery = useQuery({
    queryKey: ["investment-accounts", { is_active: true }],
    queryFn: () => getInvestmentAccounts({ is_active: true }),
  });

  const accounts = accountsQuery.data ?? [];
  const selectedAccount =
    accountId != null
      ? accounts.find((a) => a.id === accountId) ?? null
      : null;

  const holdingsQuery = useQuery({
    queryKey: ["holdings", { account_id: accountId }],
    queryFn: () => listHoldings({ account_id: accountId ?? undefined }),
  });

  const holdings = holdingsQuery.data?.holdings ?? [];

  return (
    <div className="flex flex-col gap-5 mt-4">
      <AccountFilterBar
        accounts={accounts}
        selectedAccountId={accountId}
        onSelectAccount={handleSelectAccount}
        isLoading={accountsQuery.isPending}
      />

      <HoldingsSummaryCards
        holdings={holdings}
        selectedAccount={selectedAccount}
        isLoading={holdingsQuery.isPending}
      />

      <HoldingsTable
        accountId={accountId}
        holdings={holdings}
        isLoading={holdingsQuery.isPending}
        isError={holdingsQuery.isError}
      />
    </div>
  );
}
