import { http, HttpResponse } from "msw";
import type {
  AccountRead,
  AuthConfig,
  AuthUser,
  BenchmarkRefreshSummary,
  CategoryRead,
  CategoryTreeRead,
  LabelRead,
  MerchantAliasRead,
  MerchantRuleRead,
  NavRefreshSummary,
  RuleWriteResult,
  TransactionRead,
} from "@/lib/api/client";

export const mockUser: AuthUser = {
  id: "u-test-1",
  email: "test@example.com",
  display_name: "Test User",
};

export const mockAccounts: AccountRead[] = [
  {
    id: 1,
    name: "HDFC Salary Bank",
    type: "bank",
    issuer: "HDFC Bank",
    last4: "1234",
    opening_balance_paise: 5000000,
    currency: "INR",
    parent_account_id: null,
    archived_at: null,
  },
  {
    id: 2,
    name: "ICICI Amazon Pay CC",
    type: "credit_card",
    issuer: "ICICI Bank",
    last4: "5678",
    opening_balance_paise: 0,
    currency: "INR",
    parent_account_id: 1,
    archived_at: null,
  },
];

export const mockCategories: CategoryRead[] = [
  {
    id: 1,
    name: "Food & Dining",
    kind: "spend",
    is_seeded: true,
    archived_at: null,
    color: "#ef4444",
    parent_id: null,
  },
  {
    id: 2,
    name: "Groceries",
    kind: "spend",
    is_seeded: true,
    archived_at: null,
    color: null,
    parent_id: 1,
  },
  {
    id: 3,
    name: "Salary",
    kind: "income",
    is_seeded: true,
    archived_at: null,
    color: "#22c55e",
    parent_id: null,
  },
];

export const mockCategoryTree: CategoryTreeRead[] = [
  {
    id: 1,
    name: "Food & Dining",
    kind: "spend",
    is_seeded: true,
    archived_at: null,
    color: "#ef4444",
    parent_id: null,
    subcategories: [
      {
        id: 2,
        name: "Groceries",
        kind: "spend",
        is_seeded: true,
        archived_at: null,
        color: null,
        parent_id: 1,
      },
    ],
  },
  {
    id: 3,
    name: "Salary",
    kind: "income",
    is_seeded: true,
    archived_at: null,
    color: "#22c55e",
    parent_id: null,
    subcategories: [],
  },
];

export const mockLabels: LabelRead[] = [
  { id: 1, name: "tax-deductible" },
  { id: 2, name: "reimbursable" },
];

export const mockRules: MerchantRuleRead[] = [
  {
    merchant_normalized: "swiggy",
    alias_count: 1,
    seeded: false,
    categories: [
      {
        id: 1,
        category_id: 1,
        category_name: "Food & Dining",
        parent_id: null,
        parent_name: null,
        hit_count: 12,
        last_used: "2026-08-01T12:00:00Z",
        is_winner: true,
        pinned: true,
      },
    ],
    labels: [
      {
        id: 1,
        label_id: 2,
        label_name: "reimbursable",
        hit_count: 5,
        last_used: "2026-08-01T12:00:00Z",
        prefills: true,
        prefill_threshold: 3,
        pinned: false,
      },
    ],
  },
];

export const mockMerchants: string[] = ["swiggy", "zomato", "amazon", "uber", "blinkit"];

export const mockAliases: MerchantAliasRead[] = [
  {
    id: 1,
    pattern: "swiggy india",
    canonical: "swiggy",
    is_seeded: false,
  },
];

export const mockTransactions: TransactionRead[] = [
  {
    id: 1,
    account_id: 2,
    date: "2026-08-10",
    amount_paise: -45000,
    transaction_type: "spend",
    merchant_raw: "SWIGGY BANGALORE",
    category_id: 1,
    category_name: "Food & Dining",
    category_parent_name: null,
    transfer_pair_id: null,
    labels: [{ id: 2, name: "reimbursable" }],
  },
];

export const mockAuthConfig: AuthConfig = {
  demo_login_enabled: true,
  registration_enabled: true,
};

export const mockNavRefreshSummary: NavRefreshSummary = {
  mf_updated: 2,
  equity_updated: 2,
  unmatched: 0,
  fetch_errors: 0,
  stale_skipped: 0,
  skipped: 0,
  null_nav_count: 0,
  catalogue_staleness_days: 1,
  warnings: [],
};

export const mockBenchmarkRefreshSummary: BenchmarkRefreshSummary = {
  benchmarks_refreshed: 2,
  navs_inserted: 30,
  fetch_errors: 0,
  warnings: [],
};

export const handlers = [
  // --- Auth Endpoints ---
  http.get("*/api/v1/auth/me", () => {
    return HttpResponse.json(mockUser);
  }),

  http.post("*/api/v1/auth/login", () => {
    return HttpResponse.json(mockUser);
  }),

  http.post("*/api/v1/auth/register", () => {
    return HttpResponse.json(mockUser);
  }),

  http.post("*/api/v1/auth/logout", () => {
    return new HttpResponse(null, { status: 204 });
  }),

  http.post("*/api/v1/auth/refresh", () => {
    return HttpResponse.json({ ok: true });
  }),

  http.get("*/api/v1/auth/config", () => {
    return HttpResponse.json(mockAuthConfig);
  }),

  http.post("*/api/v1/auth/change-password", () => {
    return HttpResponse.json(mockUser);
  }),

  // --- Accounts Endpoints ---
  http.get("*/api/v1/accounts", () => {
    return HttpResponse.json(mockAccounts);
  }),

  http.post("*/api/v1/accounts", async ({ request }) => {
    const body = (await request.json()) as Partial<AccountRead>;
    const created: AccountRead = {
      id: 99,
      name: body.name ?? "New Account",
      type: body.type ?? "bank",
      issuer: body.issuer ?? null,
      last4: body.last4 ?? null,
      opening_balance_paise: body.opening_balance_paise ?? 0,
      currency: body.currency ?? "INR",
      parent_account_id: null,
      archived_at: null,
    };
    return HttpResponse.json(created, { status: 201 });
  }),

  http.patch("*/api/v1/accounts/:id", async ({ params, request }) => {
    const body = (await request.json()) as Partial<AccountRead>;
    const id = Number(params.id);
    const existing = mockAccounts.find((a) => a.id === id) ?? mockAccounts[0];
    return HttpResponse.json({ ...existing, ...body });
  }),

  http.delete("*/api/v1/accounts/:id", () => {
    return new HttpResponse(null, { status: 204 });
  }),

  // --- Categories Endpoints ---
  http.get("*/api/v1/categories", ({ request }) => {
    const url = new URL(request.url);
    if (url.searchParams.get("tree") === "true") {
      return HttpResponse.json(mockCategoryTree);
    }
    const kind = url.searchParams.get("kind");
    if (kind) {
      return HttpResponse.json(mockCategories.filter((c) => c.kind === kind));
    }
    return HttpResponse.json(mockCategories);
  }),

  http.post("*/api/v1/categories", async ({ request }) => {
    const body = (await request.json()) as Partial<CategoryRead>;
    const created: CategoryRead = {
      id: 99,
      name: body.name ?? "New Category",
      kind: body.kind ?? "spend",
      is_seeded: false,
      archived_at: null,
      color: body.color ?? null,
      parent_id: body.parent_id ?? null,
    };
    return HttpResponse.json(created, { status: 201 });
  }),

  http.patch("*/api/v1/categories/:id", async ({ params, request }) => {
    const body = (await request.json()) as Partial<CategoryRead>;
    const id = Number(params.id);
    const existing = mockCategories.find((c) => c.id === id) ?? mockCategories[0];
    return HttpResponse.json({ ...existing, ...body });
  }),

  http.delete("*/api/v1/categories/:id", () => {
    return new HttpResponse(null, { status: 204 });
  }),

  // --- Labels Endpoints ---
  http.get("*/api/v1/labels", () => {
    return HttpResponse.json(mockLabels);
  }),

  http.post("*/api/v1/labels", async ({ request }) => {
    const body = (await request.json()) as Partial<LabelRead>;
    const created: LabelRead = {
      id: 99,
      name: body.name ?? "new-label",
    };
    return HttpResponse.json(created, { status: 201 });
  }),

  http.patch("*/api/v1/labels/:id", async ({ params, request }) => {
    const body = (await request.json()) as Partial<LabelRead>;
    const id = Number(params.id);
    const existing = mockLabels.find((l) => l.id === id) ?? mockLabels[0];
    return HttpResponse.json({ ...existing, ...body });
  }),

  http.delete("*/api/v1/labels/:id", () => {
    return new HttpResponse(null, { status: 204 });
  }),

  // --- Rules Endpoints ---
  http.get("*/api/v1/rules", () => {
    return HttpResponse.json(mockRules);
  }),

  http.get("*/api/v1/rules/merchants", () => {
    return HttpResponse.json(mockMerchants);
  }),

  http.post("*/api/v1/rules/categories", async ({ request }) => {
    const body = (await request.json()) as { merchant: string; category_id: number };
    const res: RuleWriteResult = {
      id: 101,
      merchant_normalized: body.merchant.trim().toLowerCase(),
      pinned: true,
    };
    return HttpResponse.json(res);
  }),

  http.patch("*/api/v1/rules/categories/:id", async ({ params, request }) => {
    const body = (await request.json()) as { pinned: boolean };
    const res: RuleWriteResult = {
      id: Number(params.id),
      merchant_normalized: "swiggy",
      pinned: body.pinned,
    };
    return HttpResponse.json(res);
  }),

  http.delete("*/api/v1/rules/categories/:id", () => {
    return new HttpResponse(null, { status: 204 });
  }),

  http.post("*/api/v1/rules/labels", async ({ request }) => {
    const body = (await request.json()) as { merchant: string; label_id: number };
    const res: RuleWriteResult = {
      id: 102,
      merchant_normalized: body.merchant.trim().toLowerCase(),
      pinned: true,
    };
    return HttpResponse.json(res);
  }),

  http.patch("*/api/v1/rules/labels/:id", async ({ params, request }) => {
    const body = (await request.json()) as { pinned: boolean };
    const res: RuleWriteResult = {
      id: Number(params.id),
      merchant_normalized: "swiggy",
      pinned: body.pinned,
    };
    return HttpResponse.json(res);
  }),

  http.delete("*/api/v1/rules/labels/:id", () => {
    return new HttpResponse(null, { status: 204 });
  }),

  // --- Aliases Endpoints ---
  http.get("*/api/v1/rules/aliases", () => {
    return HttpResponse.json(mockAliases);
  }),

  http.post("*/api/v1/rules/aliases", async ({ request }) => {
    const body = (await request.json()) as { pattern: string; canonical: string };
    const created: MerchantAliasRead = {
      id: 99,
      pattern: body.pattern,
      canonical: body.canonical,
      is_seeded: false,
    };
    return HttpResponse.json(created, { status: 201 });
  }),

  http.patch("*/api/v1/rules/aliases/:id", async ({ params, request }) => {
    const body = (await request.json()) as { canonical: string };
    const id = Number(params.id);
    const existing = mockAliases.find((a) => a.id === id) ?? mockAliases[0];
    return HttpResponse.json({ ...existing, canonical: body.canonical });
  }),

  http.delete("*/api/v1/rules/aliases/:id", () => {
    return new HttpResponse(null, { status: 204 });
  }),

  // --- Transactions Endpoints ---
  http.get("*/api/v1/transactions", () => {
    return HttpResponse.json(mockTransactions);
  }),

  http.patch("*/api/v1/transactions/:id", async ({ params, request }) => {
    const body = (await request.json()) as Partial<TransactionRead>;
    const id = Number(params.id);
    const existing = mockTransactions.find((t) => t.id === id) ?? mockTransactions[0];
    return HttpResponse.json({ ...existing, ...body });
  }),

  http.delete("*/api/v1/transactions/:id", () => {
    return new HttpResponse(null, { status: 204 });
  }),

  http.post("*/api/v1/transactions/:id/unlink", () => {
    return new HttpResponse(null, { status: 204 });
  }),

  // --- Dashboard Endpoints ---
  http.get("*/api/v1/dashboards/tagging-stats", () => {
    return HttpResponse.json({
      total_auto_tagged: 45,
      kept: 40,
      acceptance_rate: 0.89,
      rules_count: 12,
      imported_total: 50,
      pre_tagged: 45,
      coverage_rate: 0.9,
    });
  }),

  // --- Investment / Benchmark Price Refresh Endpoints ---
  http.post("*/api/v1/instruments/refresh-navs", () => {
    return HttpResponse.json(mockNavRefreshSummary);
  }),

  http.post("*/api/v1/benchmarks/refresh", () => {
    return HttpResponse.json(mockBenchmarkRefreshSummary);
  }),
];
