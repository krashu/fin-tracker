/**
 * Investment accounts API client (ADR-0019).
 *
 * Calls `/api/v1/investment-accounts` to manage broker and depository accounts.
 */

import { request } from "@/lib/api/client";
import type {
  InvestmentAccountCreate,
  InvestmentAccountRead,
  InvestmentAccountUpdate,
} from "@/lib/types/investment-accounts";

export type ListInvestmentAccountsParams = {
  is_active?: boolean;
};

export function getInvestmentAccounts(
  params?: ListInvestmentAccountsParams,
): Promise<InvestmentAccountRead[]> {
  const qs = new URLSearchParams();
  if (params?.is_active != null) {
    qs.set("is_active", String(params.is_active));
  }
  const s = qs.toString();
  return request<InvestmentAccountRead[]>(
    `/investment-accounts${s ? `?${s}` : ""}`,
  );
}

export function getInvestmentAccount(
  id: number,
): Promise<InvestmentAccountRead> {
  return request<InvestmentAccountRead>(`/investment-accounts/${id}`);
}

export function createInvestmentAccount(
  body: InvestmentAccountCreate,
): Promise<InvestmentAccountRead> {
  return request<InvestmentAccountRead>("/investment-accounts", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function updateInvestmentAccount(
  id: number,
  body: InvestmentAccountUpdate,
): Promise<InvestmentAccountRead> {
  return request<InvestmentAccountRead>(`/investment-accounts/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function deleteInvestmentAccount(id: number): Promise<void> {
  return request<void>(`/investment-accounts/${id}`, {
    method: "DELETE",
  });
}
