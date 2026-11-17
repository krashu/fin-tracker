import type {
  AccountRead,
  AuthUser,
  CategoryRead,
  CategoryTreeRead,
  ImportSummary,
  IntradayTradeRead,
  LabelRead,
  MerchantAliasRead,
  MerchantRuleRead,
  TransactionRead,
} from "@/lib/api/client";

let idCounter = 1000;
function nextId(): number {
  return ++idCounter;
}

export function buildAccount(overrides: Partial<AccountRead> = {}): AccountRead {
  const id = overrides.id ?? nextId();
  return {
    id,
    name: `Account ${id}`,
    type: "bank",
    issuer: "hdfc",
    last4: "4321",
    opening_balance_paise: 1000000,
    currency: "INR",
    parent_account_id: null,
    archived_at: null,
    statement_import_supported: true,
    ...overrides,
  };
}

export function buildCategory(overrides: Partial<CategoryRead> = {}): CategoryRead {
  const id = overrides.id ?? nextId();
  const color = "color" in overrides ? (overrides.color ?? null) : "#3b82f6";
  return {
    id,
    name: `Category ${id}`,
    kind: "spend",
    is_seeded: false,
    archived_at: null,
    color,
    display_color: color ?? "#3b82f6",
    parent_id: null,
    role: null,
    ...overrides,
  };
}

export function buildCategoryTree(
  overrides: Partial<CategoryTreeRead> = {},
): CategoryTreeRead {
  const root = buildCategory(overrides);
  return {
    ...root,
    subcategories: overrides.subcategories ?? [],
  };
}

export function buildLabel(overrides: Partial<LabelRead> = {}): LabelRead {
  const id = overrides.id ?? nextId();
  return {
    id,
    name: `label-${id}`,
    ...overrides,
  };
}

export function buildTransaction(
  overrides: Partial<TransactionRead> = {},
): TransactionRead {
  const id = overrides.id ?? nextId();
  return {
    id,
    account_id: overrides.account_id ?? 1,
    date: overrides.date ?? "2026-08-15",
    amount_paise: overrides.amount_paise ?? -25000, // standard spend: negative paise
    transaction_type: overrides.transaction_type ?? "spend",
    merchant_raw: overrides.merchant_raw ?? "TEST MERCHANT",
    category_id: overrides.category_id ?? 1,
    category_name: overrides.category_name ?? "Food & Dining",
    category_parent_name: overrides.category_parent_name ?? null,
    transfer_pair_id: overrides.transfer_pair_id ?? null,
    refund_of_id: overrides.refund_of_id ?? null,
    card_bill_link_offered: overrides.card_bill_link_offered ?? false,
    labels: overrides.labels ?? [],
    ...overrides,
  };
}

export function buildMerchantRule(
  overrides: Partial<MerchantRuleRead> = {},
): MerchantRuleRead {
  return {
    merchant_canonical: overrides.merchant_canonical ?? "swiggy",
    alias_count: overrides.alias_count ?? 1,
    seeded: overrides.seeded ?? false,
    categories: overrides.categories ?? [
      {
        id: nextId(),
        category_id: 1,
        category_name: "Food & Dining",
        parent_id: null,
        parent_name: null,
        hit_count: 5,
        last_used: "2026-08-15T10:00:00Z",
        is_winner: true,
        pinned: false,
      },
    ],
    labels: overrides.labels ?? [],
  };
}

export function buildMerchantAlias(
  overrides: Partial<MerchantAliasRead> = {},
): MerchantAliasRead {
  const id = overrides.id ?? nextId();
  return {
    id,
    pattern: overrides.pattern ?? "swiggy india",
    canonical: overrides.canonical ?? "swiggy",
    is_seeded: overrides.is_seeded ?? false,
    ...overrides,
  };
}

export function buildAuthUser(overrides: Partial<AuthUser> = {}): AuthUser {
  return {
    id: "u-test-1",
    email: "test@example.com",
    display_name: "Test User",
    is_guest: false,
    can_manage_credentials: true,
    ...overrides,
  };
}

export function buildIntradayTrade(
  overrides: Partial<IntradayTradeRead> = {},
): IntradayTradeRead {
  return {
    sell_txn_id: nextId(),
    buy_txn_id: nextId(),
    instrument_id: 1,
    symbol: "INFY",
    name: "Infosys",
    date: "2026-03-03",
    units: "50",
    buy_value_paise: 600000,
    sell_value_paise: 650000,
    sell_charges_paise: 500,
    profit_paise: 49500,
    ...overrides,
  };
}

export function buildImportSummary(overrides: Partial<ImportSummary> = {}): ImportSummary {
  return {
    batch_id: 1,
    imported: 0,
    skipped: 0,
    zero_amount_count: 0,
    already_imported: false,
    pending_count: 0,
    duplicate_of_account_id: null,
    duplicate_of_account_archived: false,
    reconciliation_delta_paise: null,
    ...overrides,
  };
}
