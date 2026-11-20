/**
 * Types for investment accounts (ADR-0019).
 *
 * Hand-mirrored from `backend/app/schemas/investment_account.py`.
 */

export type InvestmentAccountType =
  | "demat"
  | "mf_folio"
  | "us_broker"
  | "bank_fd"
  | "nps"
  | "manual";

export type InvestmentAccountRead = {
  id: number;
  user_id?: string;
  name: string;
  account_type: InvestmentAccountType;
  provider: string;
  external_account_ref: string;
  currency: "INR" | "USD";
  is_active: boolean;
  created_at?: string;
  updated_at?: string;
};

export type InvestmentAccountCreate = {
  name: string;
  account_type: InvestmentAccountType;
  provider: string;
  external_account_ref?: string;
  currency?: "INR" | "USD";
  is_active?: boolean;
};

export type InvestmentAccountUpdate = {
  name?: string;
  is_active?: boolean;
};
