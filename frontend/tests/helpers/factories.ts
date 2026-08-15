import type {
  AccountRead,
  CategoryRead,
  CategoryTreeRead,
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
    issuer: "Test Bank",
    last4: "4321",
    opening_balance_paise: 1000000,
    currency: "INR",
    parent_account_id: null,
    archived_at: null,
    ...overrides,
  };
}

export function buildCategory(overrides: Partial<CategoryRead> = {}): CategoryRead {
  const id = overrides.id ?? nextId();
  return {
    id,
    name: `Category ${id}`,
    kind: "spend",
    is_seeded: false,
    archived_at: null,
    color: "#3b82f6",
    parent_id: null,
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
    labels: overrides.labels ?? [],
    ...overrides,
  };
}

export function buildMerchantRule(
  overrides: Partial<MerchantRuleRead> = {},
): MerchantRuleRead {
  return {
    merchant_normalized: overrides.merchant_normalized ?? "swiggy",
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
