import { http, HttpResponse } from "msw";
import type {
  AccountRead,
  AssetClassCurrencies,
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
  StatementIssuer,
  TransactionRead,
} from "@/lib/api/client";

export const mockUser: AuthUser = {
  id: "u-test-1",
  email: "test@example.com",
  display_name: "Test User",
  is_guest: false,
  can_manage_credentials: true,
};

export const mockAccounts: AccountRead[] = [
  {
    id: 1,
    name: "HDFC Salary Bank",
    type: "bank",
    issuer: "hdfc",
    last4: "1234",
    opening_balance_paise: 5000000,
    currency: "INR",
    parent_account_id: null,
    archived_at: null,
    statement_import_supported: true,
  },
  {
    id: 2,
    name: "ICICI Amazon Pay CC",
    type: "credit_card",
    issuer: "icici",
    last4: "5678",
    opening_balance_paise: 0,
    currency: "INR",
    parent_account_id: 1,
    archived_at: null,
    statement_import_supported: true,
  },
];

export const mockStatementIssuers: StatementIssuer[] = [
  { account_type: "credit_card", issuer: "axis", label: "Axis" },
  { account_type: "credit_card", issuer: "icici", label: "ICICI" },
  { account_type: "bank", issuer: "hdfc", label: "HDFC" },
];

/** The server's asset-class -> currency rule (`GET /instruments/asset-classes`), in its order. */
export const mockAssetClassCurrencies: AssetClassCurrencies[] = [
  { asset_class: "indian_equity", currencies: ["INR"] },
  { asset_class: "indian_mf", currencies: ["INR"] },
  { asset_class: "us_equity", currencies: ["USD"] },
  { asset_class: "us_etf", currencies: ["USD"] },
  { asset_class: "fd", currencies: ["INR", "USD"] },
  { asset_class: "bond", currencies: ["INR", "USD"] },
  { asset_class: "nps", currencies: ["INR"] },
  { asset_class: "gold", currencies: ["INR", "USD"] },
  { asset_class: "other", currencies: ["INR", "USD"] },
];

export const mockCategories: CategoryRead[] = [
  {
    id: 1,
    name: "Food & Dining",
    kind: "spend",
    is_seeded: true,
    archived_at: null,
    color: "#ef4444",
    display_color: "#ef4444",
    parent_id: null,
    role: null,
  },
  {
    id: 2,
    name: "Groceries",
    kind: "spend",
    is_seeded: true,
    archived_at: null,
    color: null,
    display_color: "#ef4444",
    parent_id: 1,
    role: null,
  },
  {
    id: 3,
    name: "Salary",
    kind: "income",
    is_seeded: true,
    archived_at: null,
    color: "#22c55e",
    display_color: "#22c55e",
    parent_id: null,
    role: null,
  },
];

export const mockCategoryPalette = {
  colors: [
    "#2a78d6",
    "#008300",
    "#d55181",
    "#c98500",
    "#1baf7a",
    "#d95926",
    "#6c5cd6",
    "#e34948",
    "#0e97c4",
    "#c23b6b",
    "#6f9e15",
    "#b246c0",
    "#a9682f",
    "#0e9488",
  ],
};

export const mockCategoryTree: CategoryTreeRead[] = [
  {
    id: 1,
    name: "Food & Dining",
    kind: "spend",
    is_seeded: true,
    archived_at: null,
    color: "#ef4444",
    display_color: "#ef4444",
    parent_id: null,
    role: null,
    subcategories: [
      {
        id: 2,
        name: "Groceries",
        kind: "spend",
        is_seeded: true,
        archived_at: null,
        color: null,
        display_color: "#ef4444",
        parent_id: 1,
        role: null,
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
    display_color: "#22c55e",
    parent_id: null,
    role: null,
    subcategories: [],
  },
];

export const mockLabels: LabelRead[] = [
  { id: 1, name: "tax-deductible" },
  { id: 2, name: "reimbursable" },
];

export const mockRules: MerchantRuleRead[] = [
  {
    merchant_canonical: "swiggy",
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
    refund_of_id: null,
    card_bill_link_offered: false,
    labels: [{ id: 2, name: "reimbursable" }],
  },
];

export const mockAuthConfig: AuthConfig = {
  demo_login_enabled: true,
  guest_sandbox_enabled: false,
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

  http.post("*/api/v1/auth/delete-account", () => new HttpResponse(null, { status: 204 })),

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
      statement_import_supported: false,
    };
    return HttpResponse.json(created, { status: 201 });
  }),

  http.get("*/api/v1/accounts/statement-issuers", () => {
    return HttpResponse.json(mockStatementIssuers);
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
  http.get("*/api/v1/categories/palette", () => HttpResponse.json(mockCategoryPalette)),

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
    const parent =
      body.parent_id != null ? mockCategories.find((c) => c.id === body.parent_id) : undefined;
    const color = body.color ?? (body.parent_id == null ? mockCategoryPalette.colors[0] : null);
    const created: CategoryRead = {
      id: 99,
      name: body.name ?? "New Category",
      kind: body.kind ?? "spend",
      is_seeded: false,
      archived_at: null,
      color,
      display_color: color ?? parent?.display_color ?? mockCategoryPalette.colors[0],
      parent_id: body.parent_id ?? null,
      role: null,
    };
    return HttpResponse.json(created, { status: 201 });
  }),

  http.patch("*/api/v1/categories/:id", async ({ params, request }) => {
    const body = (await request.json()) as Partial<CategoryRead>;
    const id = Number(params.id);
    const existing = mockCategories.find((c) => c.id === id) ?? mockCategories[0];
    return HttpResponse.json({ ...existing, ...body, display_color: body.color ?? existing.display_color });
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

  http.post("*/api/v1/transactions/bulk-categorise", async ({ request }) => {
    const body = (await request.json()) as { transaction_ids: number[] };
    return HttpResponse.json({ updated: body.transaction_ids.length, taught: 1 });
  }),

  http.post("*/api/v1/transactions/bulk-delete", () => new HttpResponse(null, { status: 204 })),

  http.get("*/api/v1/transactions/:id/past-rows", () => {
    return HttpResponse.json({ merchant: "swiggy", category_id: null, transaction_ids: [] });
  }),

  http.post("*/api/v1/transactions/:id/unlink", () => {
    return new HttpResponse(null, { status: 204 });
  }),

  http.get("*/api/v1/transactions/:id/refund-candidates", () => {
    return HttpResponse.json([]);
  }),

  http.get("*/api/v1/transactions/:id/link-candidates", () => HttpResponse.json([])),

  http.post("*/api/v1/transactions/:id/link", () => new HttpResponse(null, { status: 204 })),

  http.put("*/api/v1/transactions/:id/refund-link", () => {
    return new HttpResponse(null, { status: 204 });
  }),

  http.delete("*/api/v1/transactions/:id/refund-link", () => {
    return new HttpResponse(null, { status: 204 });
  }),

  // --- Sample statements (guest import) ---
  http.get("*/api/v1/imports/samples", () => {
    return HttpResponse.json([
      {
        id: "axis-cc-2025-06",
        label: "Axis Bank credit card, June 2025",
        issuer: "axis",
        account_type: "credit_card",
        account_ids: [2],
      },
    ]);
  }),

  http.post("*/api/v1/imports/samples", () => {
    return HttpResponse.json({
      batch_id: 5,
      imported: 12,
      skipped: 0,
      zero_amount_count: 0,
      already_imported: false,
      pending_count: 12,
      duplicate_of_account_id: null,
      duplicate_of_account_archived: false,
      reconciliation_delta_paise: 0,
    });
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
  http.get("*/api/v1/instruments/asset-classes", () => {
    return HttpResponse.json(mockAssetClassCurrencies);
  }),

  http.post("*/api/v1/instruments/refresh-navs", () => {
    return HttpResponse.json(mockNavRefreshSummary);
  }),

  http.post("*/api/v1/benchmarks/refresh", () => {
    return HttpResponse.json(mockBenchmarkRefreshSummary);
  }),

  // --- Tax statement (F11) ---
  http.get("*/api/v1/tax/intraday-trades", () =>
    HttpResponse.json({ period: "2026", trades: [], total_profit_paise: 0 }),
  ),
  http.get("*/api/v1/tax/available-years", () =>
    HttpResponse.json({ years: [2026] }),
  ),
];
