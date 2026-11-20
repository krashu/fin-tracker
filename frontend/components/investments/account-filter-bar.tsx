"use client";

/**
 * Filter bar for investment accounts (ADR-0019, Phase 2).
 *
 * Allows switching between "All Accounts" and specific broker/depository accounts
 * (e.g., Zerodha Demat, INDmoney US Brokerage) with provider & account type badges.
 */

import { Badge } from "@/components/ui/badge";
import { Pill } from "@/components/ui/pill";
import { cn } from "@/lib/utils";
import type {
  InvestmentAccountRead,
  InvestmentAccountType,
} from "@/lib/types/investment-accounts";

const ACCOUNT_TYPE_LABELS: Record<InvestmentAccountType, string> = {
  demat: "Demat",
  mf_folio: "MF Folio",
  us_broker: "US Broker",
  bank_fd: "FD",
  nps: "NPS",
  manual: "Manual",
};

const PROVIDER_NAMES: Record<string, string> = {
  zerodha: "Zerodha",
  indmoney: "INDmoney",
  groww: "Groww",
  kuvera: "Kuvera",
  upstox: "Upstox",
  cams: "CAMS",
  kfintech: "KFintech",
  vested: "Vested",
  interactive_brokers: "Interactive Brokers",
  charles_schwab: "Charles Schwab",
};

export function formatProvider(provider: string): string {
  const lower = provider.toLowerCase();
  if (PROVIDER_NAMES[lower]) return PROVIDER_NAMES[lower];
  return provider
    .split(/[_\s]+/)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

export function formatAccountType(type: InvestmentAccountType): string {
  return ACCOUNT_TYPE_LABELS[type] ?? type;
}

export type AccountFilterBarProps = {
  accounts: InvestmentAccountRead[];
  selectedAccountId: number | null;
  onSelectAccount: (accountId: number | null) => void;
  isLoading?: boolean;
  className?: string;
};

export function AccountFilterBar({
  accounts,
  selectedAccountId,
  onSelectAccount,
  isLoading = false,
  className,
}: AccountFilterBarProps) {
  const isAllSelected =
    selectedAccountId === null ||
    (accounts.length > 0 && !accounts.some((a) => a.id === selectedAccountId));

  return (
    <nav
      aria-label="Investment account filters"
      className={cn(
        "flex flex-wrap items-center gap-2 py-3 overflow-x-auto",
        className,
      )}
    >
      <Pill
        key="all"
        active={isAllSelected}
        onClick={() => onSelectAccount(null)}
        className="cursor-pointer"
        aria-pressed={isAllSelected}
      >
        <span>All Accounts</span>
        {accounts.length > 0 ? (
          <Badge
            variant={isAllSelected ? "secondary" : "default"}
            size="sm"
            className="text-[10px]"
          >
            {accounts.length}
          </Badge>
        ) : null}
      </Pill>

      {isLoading ? (
        <span className="text-[12px] text-muted-foreground animate-pulse pl-1">
          Loading accounts…
        </span>
      ) : (
        accounts.map((acc) => {
          const isSelected = selectedAccountId === acc.id;
          const providerText = formatProvider(acc.provider);
          const typeText = formatAccountType(acc.account_type);

          return (
            <Pill
              key={acc.id}
              active={isSelected}
              onClick={() => onSelectAccount(acc.id)}
              className="cursor-pointer gap-2"
              aria-pressed={isSelected}
            >
              <span className="font-medium">{acc.name}</span>
              <div className="flex items-center gap-1">
                <Badge
                  variant={isSelected ? "secondary" : "default"}
                  size="sm"
                  className="text-[10.5px]"
                >
                  {providerText}
                </Badge>
                <Badge
                  variant="outline"
                  size="sm"
                  className={cn(
                    "text-[10px]",
                    isSelected ? "border-accent-foreground/30 text-accent-foreground" : "border-border text-muted-foreground",
                  )}
                >
                  {typeText}
                </Badge>
                {acc.currency === "USD" ? (
                  <Badge
                    variant="outline"
                    size="sm"
                    className="border-emerald-500/40 text-emerald-600 dark:text-emerald-400 text-[9.5px] px-1 font-semibold"
                  >
                    USD
                  </Badge>
                ) : null}
              </div>
            </Pill>
          );
        })
      )}
    </nav>
  );
}
