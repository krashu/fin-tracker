/**
 * Thin typed fetch wrapper over the FastAPI JSON API.
 *
 * Schemas are hand-mirrored from the backend Pydantic models in
 * `backend/app/schemas/`. This file now exports ~70 types, well past the
 * "~15 schemas" threshold an earlier version of this comment set as the
 * trigger for adopting codegen, so treat `openapi-typescript` as overdue
 * rather than hypothetical.
 *
 * Do NOT rely on tsc to catch drift. It only errors where a field is
 * structurally *used*; a backend field that changes MEANING, gains a
 * value, or is dropped from a response the UI merely passes through
 * compiles clean. That has already bitten this repo — a renderer summed
 * two of three disjoint holdings buckets and typechecked fine. Verify
 * against the backend schema, not against a green `tsc`.
 */

export type TransactionType = "spend" | "income" | "transfer";

/** A freeform user tag on a transaction (PRD §F3a — user-facing "Tags"). Stored
 * as a plain lowercased word (no leading `#`); the UI prepends `#` for display
 * and dedupes case-insensitively. `name` is unique per user. */
export type LabelRead = {
  id: number;
  name: string;
};

export type TransactionRead = {
  id: number;
  account_id: number;
  date: string;
  amount_paise: number;
  transaction_type: TransactionType;
  merchant_raw: string | null;
  category_id: number | null;
  /** The category's STORED name, resolved server-side without the `archived_at`
   * filter — so a row whose category was archived still arrives named. Needed
   * because `GET /categories` serves active rows only, which left the board
   * rendering an archived category as "Uncategorized" (a different fact, and a
   * lie about the user's data). `category_parent_name` supplies the
   * "Parent → Child" breadcrumb; it is null for a root category, and both are
   * null when `category_id` is null. Read via `categoryLabel` in
   * `lib/categories.ts`, never directly — active rows must keep resolving
   * through the live list so a rename shows up without a refetch of this row. */
  category_name: string | null;
  category_parent_name: string | null;
  /** Non-null = this row is one leg of a transfer pair (ADR-0002). Renders the
   * F4a "Linked CC bill payment" banner, and freezes the row's identity fields
   * and type until `unlinkTransaction` breaks the pair (ADR-0007 rule 7). */
  transfer_pair_id: number | null;
  labels: LabelRead[];
};

// Every user-visible column is editable (ADR-0007). Omit a key to leave it
// untouched — the dialog sends only what changed (minimal-PATCH discipline).
//
// `labels` is a REPLACE-SET: when present, the txn's label set becomes exactly
// this list of names (get-or-created server-side); an empty array clears them.
//
// `merchant_raw: null` clears the merchant; the other identity fields are NOT
// NULL server-side and an explicit null 422s. Editing any of `date`,
// `amount_paise`, `merchant_raw` or `account_id` recomputes the PRD §F4
// fingerprint, so the response can be a 409 ("transaction already exists").
// A type change across the income/spend kind boundary must carry a compatible
// `category_id` — or an explicit null — in the SAME request, else 422.
export type TransactionUpdate = {
  date?: string;
  amount_paise?: number;
  merchant_raw?: string | null;
  account_id?: number;
  transaction_type?: TransactionType;
  labels?: string[];
  category_id?: number | null;
};

// "seeded" (ADR-0011 merchant-alias layer, Phase A3): the (canonical, category)
// pair is present in the merchant map at hit_count === 0 — a dictionary entry
// this user has never confirmed — distinct from "none" (no rule at all). Must
// move together with tag-picker.tsx's `TagConfidence` (structurally, not by a
// shared import — see review-queue.tsx's TagPicker usage).
export type ConfidenceLabel = "confident" | "uncertain" | "seeded" | "none";

/** A pending import row: TransactionRead + the F3 auto-tag confidence signal. */
export type TransactionCandidate = TransactionRead & {
  prior_matches: number;
  confidence: ConfidenceLabel;
  /** Winning rule for this (merchant, category) is user-authored (pinned) —
   * the picker renders an "authored" state that outranks the confidence tint. */
  pinned: boolean;
  /** Row is `income` and its merchant names a card-bill payment (PRD §F4a-1) —
   * the review queue can offer "Card bill payment" instead of a category.
   * Does NOT assert the auto-link will fire at commit; that also needs a
   * matching bank debit and an imported parent account. */
  cc_payment_candidate: boolean;
};

/** Response body of `POST /imports`. `already_imported` means the file hash was
 * seen before (not that the upload was a no-op — a re-upload reconciles and can
 * re-stage rows missing from expenses). `pending_count` is the batch's rows still
 * awaiting review; route on it (>0 → review queue, 0 → nothing to review).
 * `duplicate_of_account_id` is *an* OTHER account this exact file was already
 * imported into (the wrong-account mis-import the per-account dedup can't catch) —
 * an id, never a name; `duplicate_of_account_archived` flags that `GET /accounts`
 * won't return it, so it can't be resolved to a label.
 * `reconciliation_delta_paise` (PRD §F1/§F4a) is this import's statement-balance
 * check as of upload: `null` = not checked (no usable statement metadata),
 * `0` = reconciled, non-zero = mismatch, this many paise. */
export type ImportSummary = {
  batch_id: number;
  imported: number;
  skipped: number;
  already_imported: boolean;
  pending_count: number;
  duplicate_of_account_id: number | null;
  duplicate_of_account_archived: boolean;
  reconciliation_delta_paise: number | null;
};

/** Response body of `POST /imports/investments` (PRD §F7). `warnings` are PII-safe
 * (line number + reason; never raw cells). `already_imported` flags an identical
 * re-upload (matched by source_file_hash) — nothing new was added. */
export type InvestmentCsvImportSummary = {
  batch_id: number;
  instruments_new: number;
  txns_imported: number;
  txns_skipped_dupe: number;
  rows_rejected: number;
  already_imported: boolean;
  warnings: string[];
};

export type AccountRead = {
  id: number;
  name: string;
  type: "credit_card" | "bank" | "cash" | "investment";
  issuer: string | null;
  last4: string | null;
  opening_balance_paise: number;
  currency: "INR" | "USD";
  parent_account_id: number | null;
  archived_at: string | null;
};

/** Spend categories serve `spend`-typed transactions, of either sign (a refund
 * is a positive `spend`); income categories serve `income`. Set at create,
 * immutable thereafter (the backend has no PATCH kind). */
export type CategoryKind = "spend" | "income";

/** A user-picked `#rrggbb` hex color for a category's dot/bar (the backend
 * validates and lower-cases it). `null` = derive the color from the id — the
 * Auto fallback in `lib/categories.ts`. A plain string alias: the value is an
 * arbitrary hex, not a closed set. */
export type CategoryColor = string;

export type CategoryRead = {
  id: number;
  name: string;
  kind: CategoryKind;
  is_seeded: boolean;
  archived_at: string | null;
  color: CategoryColor | null;
  parent_id: number | null;
};

/** Shape of a `GET /categories?tree=true` row — see `listCategoryTree`. Kept
 * distinct from `lib/categories.ts`'s `CategoryTreeNode` (built client-side by
 * `buildCategoryTree` from a flat list): this one is what the *backend*
 * returns when asked to nest server-side. Structurally identical today; that
 * is a coincidence of a 2-level taxonomy, not a contract — don't collapse
 * them into a shared import. */
export type CategoryTreeRead = CategoryRead & {
  subcategories: CategoryRead[];
};

const BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000/api/v1";

// SameSite=Lax cookies only ride the fetch when the page and the API are the same
// site — `localhost` and `127.0.0.1` count as *different* sites, so a mismatch on
// that axis silently drops the auth cookies (no error, just perpetual 401s). Warn
// loudly in dev rather than let someone lose an afternoon to it.
if (process.env.NODE_ENV !== "production" && typeof window !== "undefined") {
  const pageHost = window.location.hostname; // "localhost" | "127.0.0.1" | ...
  const apiHost = (() => {
    try {
      return new URL(BASE_URL).hostname;
    } catch {
      return "";
    }
  })();
  const loopback = new Set(["localhost", "127.0.0.1"]);
  if (loopback.has(pageHost) && loopback.has(apiHost) && pageHost !== apiHost) {
    console.warn(
      `[fin-tracker] Frontend host (${pageHost}) and API host (${apiHost}) differ ` +
        `on the localhost/127.0.0.1 axis — SameSite=Lax auth cookies won't ride ` +
        `requests, so you'll see constant 401s. Point NEXT_PUBLIC_API_BASE_URL at ` +
        `http://${pageHost}:8000/api/v1.`,
    );
  }
}

/** Carries the HTTP status + server `detail` so callers can branch on 404/409/422. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
    /**
     * The whole parsed JSON error body (the `{ detail: ... }` envelope), when the
     * response had one. Lets callers reach a structured detail — e.g. the commit
     * route's `{ message, invalid_ids }` — that `detail` (a string) can't carry.
     */
    readonly body?: unknown,
  ) {
    super(detail);
    this.name = "ApiError";
  }
}

/**
 * Build an `ApiError` from a non-OK response, preserving the full parsed body.
 *
 * FastAPI error bodies are `{ detail: string }` (our custom HTTPExceptions),
 * `{ detail: ValidationError[] }` (422 validation), or `{ detail: {...} }` (a
 * custom structured detail, e.g. the commit route's `{ message, invalid_ids }`).
 * `detail` collapses the first two to one message; the whole body stays on
 * `.body` so callers can reach structured fields. Used by both `request()` and
 * the multipart upload so every `ApiError` carries `body`.
 */
async function toApiError(res: Response): Promise<ApiError> {
  let detail = res.statusText;
  let body: unknown;
  try {
    body = await res.json();
    const d = (body as { detail?: unknown }).detail;
    if (typeof d === "string") detail = d;
    else if (Array.isArray(d) && d.length > 0) {
      const first = d[0] as { msg?: string };
      detail = first.msg ?? detail;
    }
  } catch {
    // Non-JSON error body — keep statusText, leave body undefined.
  }
  return new ApiError(res.status, detail, body);
}

// --- Auth session plumbing (PRD §Users & access v2) ---------------------------
// Tokens live in httpOnly cookies (JS can't read them), so every call must send
// credentials, and an expired-but-refreshable access token is handled here rather
// than at each call site: a 401 triggers one silent refresh + replay.

/** Set by the auth provider; invoked when a refresh fails so the UI can drop the
 * session (→ guard redirects). Returns an unregister fn to avoid stale closures
 * under Fast Refresh / in tests. No navigation happens in this module. */
let onAuthFailure: (() => void) | null = null;

export function setAuthFailureHandler(fn: () => void): () => void {
  onAuthFailure = fn;
  return () => {
    if (onAuthFailure === fn) onAuthFailure = null;
  };
}

// Single-flight refresh: concurrent 401s share one in-flight POST /auth/refresh
// instead of stampeding the endpoint (and racing rotation). Resolves true on a
// reissued access cookie, false otherwise.
let refreshInFlight: Promise<boolean> | null = null;

function tryRefresh(): Promise<boolean> {
  if (refreshInFlight === null) {
    refreshInFlight = fetch(`${BASE_URL}/auth/refresh`, {
      method: "POST",
      credentials: "include",
    })
      .then((res) => res.ok)
      .catch(() => false) // transport error → treat as un-refreshable
      .finally(() => {
        refreshInFlight = null;
      });
  }
  return refreshInFlight;
}

/**
 * The one transport. Always sends cookies; on a 401 (unless `noRefresh`) attempts
 * one silent refresh and replays once, firing `onAuthFailure` when the session is
 * truly dead. Returns the raw `Response`: it forces no `content-type` and parses no
 * body, so multipart uploads and blob downloads share the session contract instead
 * of each reimplementing the fetch and implementing none of it.
 *
 * The replay re-sends `init.body`. Safe for the shapes we pass — `string` and
 * `FormData` are re-readable, so a fresh Request is built from the same object. A
 * `ReadableStream` body would not be; don't introduce one without buffering first.
 */
async function sendWithRefresh(
  path: string,
  init?: RequestInit,
  opts?: { noRefresh?: boolean },
): Promise<Response> {
  const doFetch = () =>
    fetch(`${BASE_URL}${path}`, { ...init, credentials: "include" });

  let res = await doFetch();
  if (res.status === 401 && !opts?.noRefresh) {
    if (await tryRefresh()) {
      res = await doFetch();
      // A 401 that survives a *successful* refresh means the session is truly
      // dead (rotation race, user row gone) — drop it, don't leave a zombie
      // "authenticated" state on non-/me queries with no redirect.
      if (res.status === 401) onAuthFailure?.();
    } else {
      onAuthFailure?.();
    }
  }
  return res;
}

/**
 * Core JSON fetch: `sendWithRefresh` plus the JSON `content-type` and body parse.
 * `noRefresh` is used by the auth endpoints themselves (a login 401 is bad creds,
 * not an expired session, and the refresh call must never recurse).
 */
async function request<T>(
  path: string,
  init?: RequestInit,
  opts?: { noRefresh?: boolean },
): Promise<T> {
  const res = await sendWithRefresh(
    path,
    {
      ...init,
      headers: { "content-type": "application/json", ...init?.headers },
    },
    opts,
  );
  if (!res.ok) throw await toApiError(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// --- Auth (PRD §Users & access v2) --------------------------------------------
// Tokens are never in a response body — the backend sets httpOnly cookies. These
// return the user shape only. login/register/logout pass `noRefresh` so a 401
// (bad creds) or 409 surfaces as-is instead of triggering a refresh; `me()` omits
// it so an expired-access + valid-refresh session restores transparently.

export type AuthUser = {
  id: string;
  email: string | null;
  display_name: string | null;
};

/** Public demo credentials (backend `app/core/demo.py`) — the demo owns only
 * synthetic sample data with placeholder card last-4s, so these are intentionally
 * shipped in the client for the "Try the demo" button. */
export const DEMO_CREDENTIALS = {
  email: "demo@fin-tracker.local",
  password: "demofintracker",
} as const;

export function login(email: string, password: string): Promise<AuthUser> {
  return request<AuthUser>(
    "/auth/login",
    { method: "POST", body: JSON.stringify({ email, password }) },
    { noRefresh: true },
  );
}

export function register(
  email: string,
  password: string,
  displayName?: string,
): Promise<AuthUser> {
  return request<AuthUser>(
    "/auth/register",
    {
      method: "POST",
      body: JSON.stringify({
        email,
        password,
        display_name: displayName?.trim() || null,
      }),
    },
    { noRefresh: true },
  );
}

export function logout(): Promise<void> {
  return request<void>("/auth/logout", { method: "POST" }, { noRefresh: true });
}

export function me(): Promise<AuthUser> {
  return request<AuthUser>("/auth/me");
}

/** Change the signed-in user's password. The backend re-hashes, revokes every
 * other device's refresh session, and sets fresh cookies so this device stays in.
 * Participates in refresh (no `noRefresh`): a stale access token here should
 * silently refresh + replay, not surface as an error. 400 on a wrong or unchanged
 * current password (`ApiError.detail` carries the reason). */
export function changePassword(
  currentPassword: string,
  newPassword: string,
): Promise<AuthUser> {
  return request<AuthUser>("/auth/change-password", {
    method: "POST",
    body: JSON.stringify({
      current_password: currentPassword,
      new_password: newPassword,
    }),
  });
}

/** Public, pre-auth client config. `demo_login_enabled` mirrors the backend's resolved
 * demo-login gate — off unless the operator set `DEMO_LOGIN_ENABLED` on a plain-http
 * deploy — so the login page can hide the "Try the demo" button when it wouldn't work.
 * `noRefresh`: this is read before any session exists, so a 401 must never trigger a
 * refresh attempt. */
export type AuthConfig = { demo_login_enabled: boolean };

export function getAuthConfig(): Promise<AuthConfig> {
  return request<AuthConfig>("/auth/config", undefined, { noRefresh: true });
}

export type ListTransactionsParams = {
  transaction_type?: TransactionType[];
  // Orthogonal to transaction_type, not a replacement for it — composes as an
  // AND. `{transaction_type: ["spend"], amount_sign: "positive"}` is the
  // refund-only view now that refund is a sign on a spend row, not its own type.
  amount_sign?: "positive" | "negative";
  account_id?: number;
  category_id?: number;
  label_id?: number;
  date_from?: string;
  date_to?: string;
  limit?: number;
  offset?: number;
};

function buildQuery(params: ListTransactionsParams): string {
  const qs = new URLSearchParams();
  // Repeated key for the list-valued filter: ?transaction_type=spend&transaction_type=income.
  // Omit entirely when absent — sending an empty value 422s on the Literal.
  params.transaction_type?.forEach((t) => qs.append("transaction_type", t));
  if (params.amount_sign) qs.set("amount_sign", params.amount_sign);
  if (params.account_id != null)
    qs.set("account_id", String(params.account_id));
  if (params.category_id != null)
    qs.set("category_id", String(params.category_id));
  if (params.label_id != null) qs.set("label_id", String(params.label_id));
  if (params.date_from) qs.set("date_from", params.date_from);
  if (params.date_to) qs.set("date_to", params.date_to);
  if (params.limit != null) qs.set("limit", String(params.limit));
  if (params.offset != null) qs.set("offset", String(params.offset));
  const s = qs.toString();
  return s ? `?${s}` : "";
}

export function listTransactions(
  params: ListTransactionsParams = {},
): Promise<TransactionRead[]> {
  return request<TransactionRead[]>(`/transactions${buildQuery(params)}`);
}

export function listAccounts(): Promise<AccountRead[]> {
  return request<AccountRead[]>("/accounts");
}

// Every call site today passes no params, so the shared `["categories"]`
// TanStack key (used at all 11 call sites) is safe — a future caller that
// passes `kind` (or calls `listCategoryTree`) must fold the params into its
// own query key (e.g. `["categories", params]`) or it will collide with the
// unfiltered cache entry.
export function listCategories(params?: {
  kind?: CategoryKind;
}): Promise<CategoryRead[]> {
  const qs = new URLSearchParams();
  if (params?.kind) {
    qs.set("kind", params.kind);
  }
  const s = qs.toString();
  return request<CategoryRead[]>(`/categories${s ? `?${s}` : ""}`);
}

/** Same endpoint, `tree=true`: each root comes back with its subcategories
 * nested, a shape `CategoryRead[]` can't express — split out rather than
 * overloaded so the return type is never a lie (AGENTS.md §The tsc blind
 * spot). No caller passes `tree` yet; this exists so the next one that does
 * gets a real type instead of reaching for `as`. */
export function listCategoryTree(params?: {
  kind?: CategoryKind;
}): Promise<CategoryTreeRead[]> {
  const qs = new URLSearchParams();
  if (params?.kind) {
    qs.set("kind", params.kind);
  }
  qs.set("tree", "true");
  return request<CategoryTreeRead[]>(`/categories?${qs.toString()}`);
}

// --- Account mutations (PRD §F6) ----------------------------------------------
// `type`/`currency`/`opening_balance_paise` are set once at create and locked
// thereafter (the backend rejects them on PATCH with 422), so AccountUpdate
// omits them. `parent_account_id` (CC→bank linking, F4a) is PATCH-only: it is
// absent from AccountCreate on purpose, because the five-rule link gate needs
// the stored `type` to validate against (see the backend's
// `_assert_parent_account_or_422`).
export type AccountCreate = {
  name: string;
  type: AccountRead["type"];
  issuer?: string | null;
  last4?: string | null;
  opening_balance_paise?: number;
  currency?: AccountRead["currency"];
};

// An explicit `null` on `parent_account_id` is meaningful — it UNLINKS a
// previously-set CC→bank parent. Omit the key to leave the link untouched
// (minimal-PATCH discipline, mirroring TransactionUpdate's `labels`).
export type AccountUpdate = {
  name?: string;
  issuer?: string | null;
  last4?: string | null;
  parent_account_id?: number | null;
};

export function createAccount(body: AccountCreate): Promise<AccountRead> {
  return request<AccountRead>("/accounts", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function patchAccount(
  id: number,
  body: AccountUpdate,
): Promise<AccountRead> {
  return request<AccountRead>(`/accounts/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

/** Soft-delete an account (204 → archived_at set). Transactions stay linked;
 * the name is freed for reuse. Throws `ApiError(404)` if already archived. */
export function deleteAccount(id: number): Promise<void> {
  return request<void>(`/accounts/${id}`, { method: "DELETE" });
}

// --- Category mutations (PRD §F5) ---------------------------------------------
// `color` is optional on create and nullable on update — an explicit `null`
// PATCH clears a picked color and reverts to derive-from-id (unlike `name`,
// which can't be nulled).
export type CategoryCreate = {
  name: string;
  kind?: CategoryKind;
  color?: CategoryColor | null;
  parent_id?: number | null;
};
export type CategoryUpdate = {
  name?: string;
  color?: CategoryColor | null;
  parent_id?: number | null;
};

export function createCategory(body: CategoryCreate): Promise<CategoryRead> {
  return request<CategoryRead>("/categories", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function patchCategory(
  id: number,
  body: CategoryUpdate,
): Promise<CategoryRead> {
  return request<CategoryRead>(`/categories/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

/** Soft-delete a category (204). Also clears its merchant→category auto-tag
 * mappings server-side. Throws `ApiError(404)` if already archived. */
export function deleteCategory(id: number): Promise<void> {
  return request<void>(`/categories/${id}`, { method: "DELETE" });
}

// --- Label mutations (PRD §F3a — user "Tags") ---------------------------------
// Freeform transaction tags. On the txn write path names are normalized and
// get-or-created, so these routes manage the catalog itself: the settings list,
// the rename/delete surface, and the autocomplete source. Names are stored plain
// (no `#`); `name` collisions 409. Delete is a HARD delete — it cascades to the
// join rows, removing the tag from every transaction that carried it.
export type LabelCreate = { name: string };
export type LabelUpdate = { name: string };

export function listLabels(): Promise<LabelRead[]> {
  return request<LabelRead[]>("/labels");
}

export function createLabel(body: LabelCreate): Promise<LabelRead> {
  return request<LabelRead>("/labels", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function patchLabel(id: number, body: LabelUpdate): Promise<LabelRead> {
  return request<LabelRead>(`/labels/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function deleteLabel(id: number): Promise<void> {
  return request<void>(`/labels/${id}`, { method: "DELETE" });
}

// --- Auto-tag rules (PRD §F3 / §F3a) ------------------------------------------
// The read + author + prune surface for the two per-merchant memory tables the
// import pipeline learns from: merchant→category (`merchant_tag_map`, one winner
// prefilled) and merchant→label (`merchant_label_map`, each auto-applies once it
// clears the prefill threshold OR is pinned). The user can *pin* a rule so it
// always wins regardless of hit_count (authored, not learned); pin/un-pin toggle
// only the flag, deleting forgets the association entirely (it re-learns on the
// next confirm of that merchant). NOT F4a reconciliation, NOT regex rules (v1).
export type CategoryRuleRead = {
  id: number; // merchant_tag_map.id — the delete / pin handle
  category_id: number;
  category_name: string;
  // The backend always sends both (rules.py's outerjoin projects `NULL`, not
  // an absent key, for a root category) — required-but-nullable, not
  // optional, so a future drop of either field is a tsc error at every call
  // site instead of a silent `undefined` (AGENTS.md §The tsc blind spot).
  parent_id: number | null;
  parent_name: string | null;
  hit_count: number;
  last_used: string;
  // This row's category is the AGGREGATE winner for its canonical merchant
  // (ADR-0011 Phase A3: summed hit_count across every raw merchant_normalized
  // an alias folds together) — not "this exact row has the highest
  // hit_count"; two raw rows can share the winning category and both read
  // true.
  is_winner: boolean;
  pinned: boolean; // user-authored: wins regardless of hit_count
};

export type LabelRuleRead = {
  id: number; // merchant_label_map.id — the delete / pin handle
  label_id: number;
  label_name: string; // stored plain (no `#`); UI prepends it
  hit_count: number;
  last_used: string;
  prefills: boolean; // auto-applies in the import review queue
  prefill_threshold: number; // the learned-prefill bar; use for the "n/N" hint
  pinned: boolean; // user-authored: prefills even below the learned bar
};

// As of Phase A3 (ADR-0011) `merchant_normalized` is the CANONICAL merchant —
// an unaliased merchant resolves to itself, so this keeps its name but its
// value now depends on the user's alias table.
export type MerchantRuleRead = {
  merchant_normalized: string;
  categories: CategoryRuleRead[];
  labels: LabelRuleRead[];
  alias_count: number; // distinct raw merchant_normalized keys folded in; always >=1
  seeded: boolean; // every category row in the group is an unconfirmed seed (hit_count === 0)
};

// Result of a pin/create/toggle write. `merchant_normalized` is the server's
// normalized key echoed back — the UI shows it (never reimplements normalize).
export type RuleWriteResult = {
  id: number;
  merchant_normalized: string;
  pinned: boolean;
};

export function listRules(): Promise<MerchantRuleRead[]> {
  return request<MerchantRuleRead[]>("/rules");
}

// Distinct observed merchants (transactions + both maps), for the create-rule
// merchant autocomplete. Already server-normalized.
export function listRuleMerchants(): Promise<string[]> {
  return request<string[]>("/rules/merchants");
}

// Pin a merchant→category rule (create-new or re-point to a never-seen category).
export function createCategoryRule(body: {
  merchant: string;
  category_id: number;
}): Promise<RuleWriteResult> {
  return request<RuleWriteResult>("/rules/categories", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// Toggle `pinned` on an existing category rule (re-point among learned / un-pin).
export function patchCategoryRulePinned(
  id: number,
  pinned: boolean,
): Promise<RuleWriteResult> {
  return request<RuleWriteResult>(`/rules/categories/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ pinned }),
  });
}

// Pin a merchant→label rule. `label_id` must be an existing tag (no create here).
export function createLabelRule(body: {
  merchant: string;
  label_id: number;
}): Promise<RuleWriteResult> {
  return request<RuleWriteResult>("/rules/labels", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// Toggle `pinned` on an existing label rule.
export function patchLabelRulePinned(
  id: number,
  pinned: boolean,
): Promise<RuleWriteResult> {
  return request<RuleWriteResult>(`/rules/labels/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ pinned }),
  });
}

export function deleteCategoryRule(id: number): Promise<void> {
  return request<void>(`/rules/categories/${id}`, { method: "DELETE" });
}

export function deleteLabelRule(id: number): Promise<void> {
  return request<void>(`/rules/labels/${id}`, { method: "DELETE" });
}

// One user-authored `pattern -> canonical` row (ADR-0011 merchant-alias
// layer, Phase A4). `is_seeded` flags a dictionary entry from Phase A5,
// distinct from a merchant_tag_map row at hit_count === 0.
export type MerchantAliasRead = {
  id: number;
  pattern: string;
  canonical: string;
  is_seeded: boolean;
};

export function listAliases(): Promise<MerchantAliasRead[]> {
  return request<MerchantAliasRead[]>("/rules/aliases");
}

// Add a pattern -> canonical rule. The server 422s on a blank field after
// normalization, a zero-token pattern (the false-merge hazard), a duplicate
// pattern, or decision 7's no-chaining conflict in either direction.
export function createAlias(body: {
  pattern: string;
  canonical: string;
}): Promise<MerchantAliasRead> {
  return request<MerchantAliasRead>("/rules/aliases", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// Rename an alias's canonical target. `pattern` is immutable — delete and
// recreate to change it.
export function patchAliasCanonical(id: number, canonical: string): Promise<MerchantAliasRead> {
  return request<MerchantAliasRead>(`/rules/aliases/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ canonical }),
  });
}

export function deleteAlias(id: number): Promise<void> {
  return request<void>(`/rules/aliases/${id}`, { method: "DELETE" });
}

export function patchTransaction(
  id: number,
  body: TransactionUpdate,
): Promise<TransactionRead> {
  return request<TransactionRead>(`/transactions/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

/** Hard-delete a transaction (204). Throws `ApiError(404)` if already gone. */
export function deleteTransaction(id: number): Promise<void> {
  return request<void>(`/transactions/${id}`, { method: "DELETE" });
}

/** Break a transfer pair (PRD §F4a-1 "break the link if the auto-detection got it
 * wrong"). Clears `transfer_pair_id` on BOTH legs; idempotent 204 on an already
 * unpaired row. Both legs keep `transaction_type = "transfer"` — no provenance
 * column exists to restore the pre-link spend/income type — so the caller may want
 * to follow up with a PATCH. Unlinking is also the prerequisite for editing a
 * paired row's identity fields (ADR-0007 rule 7). */
export function unlinkTransaction(id: number): Promise<void> {
  return request<void>(`/transactions/${id}/unlink`, { method: "POST" });
}

/**
 * Manual transaction entry (PRD §F2). `amount_paise` is signed — the caller
 * applies the sign by entry direction (spend negative, income positive; a
 * refund is a `spend` with a positive amount — see `EntryDirection` in
 * `lib/transaction-types.ts`). Auto-confirmed server-side, so it lands on the
 * board immediately. 409 if it duplicates an existing fingerprint.
 */
export type TransactionCreate = {
  date: string;
  account_id: number;
  amount_paise: number;
  transaction_type: TransactionType;
  merchant_raw?: string | null;
  category_id?: number | null;
  labels?: string[];
};

export function createTransaction(
  body: TransactionCreate,
): Promise<TransactionRead> {
  return request<TransactionRead>("/transactions", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/**
 * Manual transfer between two of the user's accounts (PRD §F2). `amount_paise`
 * is a positive magnitude — the server derives the leg signs (source −, dest +),
 * the "Transfer to/from {name}" labels, and the `transfer_pair_id` link, and
 * auto-confirms both legs. 422 on same account / investment account / currency
 * mismatch; 409 on a duplicate.
 */
export type TransferCreate = {
  date: string;
  source_account_id: number;
  dest_account_id: number;
  amount_paise: number;
};

export type TransferRead = { source: TransactionRead; dest: TransactionRead };

export function createTransfer(body: TransferCreate): Promise<TransferRead> {
  return request<TransferRead>("/transactions/transfer", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/**
 * Upload a statement for parsing. Multipart, so it goes through `sendWithRefresh`
 * rather than `request()`: the browser must set `multipart/form-data; boundary=…`
 * itself, and `request()` forces `content-type: application/json`, which would leave
 * FastAPI's Form()/File() parsing with no boundary. Only the header and the JSON
 * parse are skipped — the 401 → refresh → replay contract still applies.
 */
export async function createImport(args: {
  account_id: number;
  file: File;
  password?: string;
}): Promise<ImportSummary> {
  const form = new FormData();
  form.append("account_id", String(args.account_id));
  form.append("file", args.file);
  // Omit when blank — absent → None server-side; an empty-string password 422s
  // an encrypted PDF.
  if (args.password) form.append("password", args.password);
  const res = await sendWithRefresh("/imports", { method: "POST", body: form });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as ImportSummary;
}

/**
 * Upload a canonical investment-transaction CSV (PRD §F7). Multipart, account-less.
 * `asset_class` is applied to rows without an asset_class column (e.g. a single-asset
 * Zerodha tradebook — the user picks the class once). Commits directly and returns a
 * summary, not a review batch.
 */
export async function createInvestmentCsvImport(args: {
  file: File;
  asset_class: AssetClass;
}): Promise<InvestmentCsvImportSummary> {
  const form = new FormData();
  form.append("file", args.file);
  form.append("asset_class", args.asset_class);
  const res = await sendWithRefresh("/imports/investments", {
    method: "POST",
    body: form,
  });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as InvestmentCsvImportSummary;
}

export function listCandidates(
  batchId: number,
): Promise<TransactionCandidate[]> {
  return request<TransactionCandidate[]>(`/imports/${batchId}/candidates`);
}

/** One open import batch (≥1 row still awaiting review) — feeds the top-bar
 * notification bell. `account_*` label the batch (both null for the
 * account-less investment batches, which never surface here as they commit
 * directly). The only batch-list surface in v1: it's the way back to a review
 * queue after navigating away from a fresh upload.
 * `reconciliation_delta_paise` is the batch's stored (not recomputed)
 * statement-balance verdict — see `ImportSummary` and `BatchReconciliation`. */
export type PendingImportBatch = {
  batch_id: number;
  account_name: string | null;
  account_last4: string | null;
  pending_count: number;
  reconciliation_delta_paise: number | null;
};

export function listPendingImports(): Promise<PendingImportBatch[]> {
  return request<PendingImportBatch[]>("/imports/pending");
}

/** Response body of `GET /imports/{batchId}/reconciliation` (PRD §F1/§F4a).
 * The backend recomputes this on every call (not just a read of a stored
 * column) — a commit or a discard since the last check can flip the verdict —
 * and persists the fresh delta before returning. `expected_paise`
 * (closing − opening) and `actual_paise` are derived server-side from
 * `delta_paise`, not fetched independently, so they can't disagree with it.
 * `status` is derived from `delta_paise`: `"unavailable"` when it's null (no
 * usable statement metadata, or an account-less batch), else `"matched"`
 * (`delta_paise === 0`) or `"mismatched"`. Every field but `batch_id`,
 * `status` and `rows_removed_since_import` is null in the "unavailable" case.
 * `rows_removed_since_import` is a discard-noise qualifier — how many of the
 * batch's originally-staged rows no longer exist (most commonly an
 * investment-transfer SIP debit discarded at review) — computed regardless
 * of `status`, but only meaningful alongside `"mismatched"`: it explains the
 * mismatch rather than leaving it a bare, unexplained number. */
export type BatchReconciliation = {
  batch_id: number;
  opening_balance_paise: number | null;
  closing_balance_paise: number | null;
  period_start: string | null;
  period_end: string | null;
  expected_paise: number | null;
  actual_paise: number | null;
  delta_paise: number | null;
  status: "unavailable" | "matched" | "mismatched";
  rows_removed_since_import: number;
};

export function getBatchReconciliation(
  batchId: number,
): Promise<BatchReconciliation> {
  return request<BatchReconciliation>(`/imports/${batchId}/reconciliation`);
}

// --- Backup (PRD §F10) --------------------------------------------------------
// `POST /backup/import` is additive: rows already present are skipped by
// recomputed fingerprint, so re-importing a backup after adding more
// transactions just tops up — it never wipes existing data.
export type BackupImportSummary = {
  batch_id: number;
  accounts_new: number;
  accounts_matched: number;
  categories_new: number;
  categories_matched: number;
  txns_imported: number;
  txns_skipped_dupe: number;
  rows_rejected: number;
  transfers_relinked: number;
  warnings: string[];
};

/**
 * Download the spend backup zip (`GET /backup`). Returns the blob plus a filename
 * derived from the response's `Content-Disposition` (dated default if absent). Stays
 * DOM-free — the caller triggers the browser save. Uses `sendWithRefresh` rather than
 * `request()` because the body is a zip, not JSON; the session contract still applies.
 */
export async function downloadBackup(): Promise<{
  blob: Blob;
  filename: string;
}> {
  const res = await sendWithRefresh("/backup");
  if (!res.ok) throw await toApiError(res);
  const disposition = res.headers.get("content-disposition") ?? "";
  const match = disposition.match(/filename="?([^"]+)"?/);
  return {
    blob: await res.blob(),
    filename: match?.[1] ?? "fin-tracker-backup.zip",
  };
}

/**
 * Load a backup zip (`POST /backup/import`). Multipart, so it goes through
 * `sendWithRefresh` rather than `request()` (the browser must set the multipart
 * boundary), keeping the 401 → refresh → replay contract. Additive +
 * non-destructive; the summary carries counts plus PII-safe per-row warnings.
 */
export async function importBackup(file: File): Promise<BackupImportSummary> {
  const form = new FormData();
  form.append("file", file);
  const res = await sendWithRefresh("/backup/import", {
    method: "POST",
    body: form,
  });
  if (!res.ok) throw await toApiError(res);
  return (await res.json()) as BackupImportSummary;
}

export function commitImport(
  batchId: number,
  transactionIds: number[],
): Promise<void> {
  return request<void>(`/imports/${batchId}/commit`, {
    method: "POST",
    body: JSON.stringify({ transaction_ids: transactionIds }),
  });
}

export function cancelImport(batchId: number): Promise<void> {
  return request<void>(`/imports/${batchId}`, { method: "DELETE" });
}

/** One bar of the spend-by-period series — `period` is "YYYY-MM" or "YYYY-Www"
 * (ISO week, Monday start); `total_paise` is signed (spend negative). */
export type SpendByPeriodBucket = { period: string; total_paise: number };

/** Response body of `GET /dashboards/spend-by-period` (PRD §F8 spend bar).
 * `buckets` is chronological ascending, zero-filled, never empty. `label_id`
 * echoes the optional F3a tag filter (null = unfiltered). */
export type SpendByPeriodResponse = {
  bucket: "week" | "month";
  start: string;
  end: string;
  buckets: SpendByPeriodBucket[];
  label_id: number | null;
};

/**
 * Signed-sum spend per ISO-week or calendar-month over [start, end] (clipped,
 * not snapped — `end=today` yields a period-to-date edge bucket). All three
 * params are required and non-empty (`bucket` is a closed Literal the UI never
 * lets go empty; start/end are local YYYY-MM-DD), so an inline URLSearchParams
 * is safe here without buildQuery's omit-when-absent guards.
 */
export function listSpendByPeriod(params: {
  bucket: "week" | "month";
  start: string;
  end: string;
  label_id?: number;
}): Promise<SpendByPeriodResponse> {
  const qs = new URLSearchParams({
    bucket: params.bucket,
    start: params.start,
    end: params.end,
  });
  if (params.label_id != null) qs.set("label_id", String(params.label_id));
  return request<SpendByPeriodResponse>(`/dashboards/spend-by-period?${qs}`);
}

/** One bar-group of the cashflow series (PRD §F8 view 3, the /spending
 * income-vs-spend chart). `period` is "YYYY-MM" or "YYYY-Www". `income_paise` ≥ 0;
 * `expense_paise` is SIGNED (≤ 0 typically, matching SpendByPeriodBucket — but
 * legitimately > 0 in a refund-dominant bucket, never server-clamped); `net_paise`
 * = income + expense (server-computed, goes negative on a deficit bucket). */
export type CashflowByPeriodBucket = {
  period: string;
  income_paise: number;
  expense_paise: number;
  net_paise: number;
};

/** Response body of `GET /dashboards/cashflow-by-period`. Same window contract as
 * `SpendByPeriodResponse` (`buckets` chronological, zero-filled, never empty). */
export type CashflowByPeriodResponse = {
  bucket: "week" | "month";
  start: string;
  end: string;
  buckets: CashflowByPeriodBucket[];
};

export function listCashflowByPeriod(params: {
  bucket: "week" | "month";
  start: string;
  end: string;
}): Promise<CashflowByPeriodResponse> {
  const qs = new URLSearchParams({
    bucket: params.bucket,
    start: params.start,
    end: params.end,
  });
  return request<CashflowByPeriodResponse>(
    `/dashboards/cashflow-by-period?${qs}`,
  );
}

/** One category in the spend-by-category-by-period echoed set (PRD §F8 view 3,
 * the /spending category-trend bar): its id + name, defining the stable stack
 * order and the color-join key. Both null for the uncategorized bucket; name is
 * additionally null for a row pointing at a foreign-user category (the guarded
 * JOIN drops the leaked name — cf. SpendByCategoryRow). */
export type SpendCategoryRef = {
  category_id: number | null;
  category_name: string | null;
};

/** One cell of the category×period grid: a category's SIGNED total in a bucket
 * (spend negative, refund positive — nets like SpendByCategoryRow). Never
 * clamped on EITHER side of the wire: the sole consumer
 * (`category-trend-bar.tsx`) plots one category at a time against a `y=0`
 * reference line, so a net-credit bucket dips below the axis. Nothing floors. */
export type SpendCategoryPeriodTotal = {
  category_id: number | null;
  total_paise: number;
};

/** One bucket of the category-trend series. `totals` is DENSE — one cell per
 * category in the response's `categories` set, zero-filled, in `categories`
 * order. */
export type SpendByCategoryByPeriodBucket = {
  period: string;
  totals: SpendCategoryPeriodTotal[];
};

/** Response body of `GET /dashboards/spend-by-category-by-period`. `categories`
 * is echoed once (dropdown/series order + color join); `buckets` is chronological
 * ascending, zero-filled, never empty. Σ(a bucket's cell totals) reconciles to
 * the matching `SpendByPeriodBucket.total_paise` (both signed, both spend+refund),
 * and the identity holds on screen too — the chart is a single signed series, not
 * a stack, so nothing is floored away. */
export type SpendByCategoryByPeriodResponse = {
  bucket: "week" | "month";
  start: string;
  end: string;
  categories: SpendCategoryRef[];
  buckets: SpendByCategoryByPeriodBucket[];
  label_id: number | null;
};

export function listSpendByCategoryByPeriod(params: {
  bucket: "week" | "month";
  start: string;
  end: string;
  label_id?: number;
}): Promise<SpendByCategoryByPeriodResponse> {
  const qs = new URLSearchParams({
    bucket: params.bucket,
    start: params.start,
    end: params.end,
  });
  if (params.label_id != null) qs.set("label_id", String(params.label_id));
  return request<SpendByCategoryByPeriodResponse>(
    `/dashboards/spend-by-category-by-period?${qs}`,
  );
}

/** One tag in the spend-by-tag-by-period echoed set (PRD §F3a labels, the
 * /spending tag-trend chart — arc Phase C): its id + name, defining the stable
 * line order and the palette-by-rank color join. Unlike SpendCategoryRef, id and
 * name are NON-null: the route inner-joins labels (foreign/orphan labels drop the
 * row) and excludes the untagged residual, so every tag here is real. */
export type TagRef = {
  label_id: number;
  label_name: string;
};

/** One cell of the tag×period grid: a tag's SIGNED total in a bucket (spend
 * negative, refund positive — nets like SpendByTagRow). Never server-clamped; a
 * net-credit bucket surfaces positive and the trend line dips below y=0. */
export type SpendByTagPeriodTotal = {
  label_id: number;
  total_paise: number;
};

/** One period of the tag-trend chart. `totals` is DENSE — one cell per tag in
 * the response's `tags` set, zero-filled, in `tags` order. */
export type SpendByTagByPeriodBucket = {
  period: string;
  totals: SpendByTagPeriodTotal[];
};

/** Response body of `GET /dashboards/spend-by-tag-by-period` (arc Phase C).
 * `tags` is echoed once (line order + color join), ordered biggest overall
 * spender first; the untagged residual is excluded. `buckets` is chronological
 * ascending, zero-filled, never empty. NOTE: unlike the category-trend response,
 * there is NO cross-tag reconciliation identity — cells double-count multi-tagged
 * txns and untagged is dropped, so Σ(a bucket's cells) does NOT equal that
 * bucket's spend-by-period total. The only valid reconciliation is per-tag. */
export type SpendByTagByPeriodResponse = {
  bucket: "week" | "month";
  start: string;
  end: string;
  tags: TagRef[];
  buckets: SpendByTagByPeriodBucket[];
};

export function listSpendByTagByPeriod(params: {
  bucket: "week" | "month";
  start: string;
  end: string;
}): Promise<SpendByTagByPeriodResponse> {
  const qs = new URLSearchParams({
    bucket: params.bucket,
    start: params.start,
    end: params.end,
  });
  return request<SpendByTagByPeriodResponse>(
    `/dashboards/spend-by-tag-by-period?${qs}`,
  );
}

/** One row of the spend-by-category aggregate (PRD §F8 view 2). `total_paise`
 * is signed (spend negative, refund positive), so a category whose refunds
 * outweigh its in-window spend surfaces positive. Both ids are null for the
 * uncategorized bucket. */
export type SpendByCategoryRow = {
  category_id: number | null;
  category_name: string | null;
  total_paise: number;
};

export type AvailableYearsResponse = {
  years: number[];
};

export function listAvailableYears(): Promise<AvailableYearsResponse> {
  return request<AvailableYearsResponse>("/dashboards/available-years");
}

/** Response body of `GET /dashboards/spend-by-category`. `period` echoes the
 * requested month (`"YYYY-MM"`) or year (`"YYYY"`) verbatim; `rows` are
 * most-negative-first (biggest spend first) with the uncategorized row pinned
 * last — server-ordered, render as received. */
export type SpendByCategoryResponse = {
  period: string;
  rows: SpendByCategoryRow[];
  label_id: number | null;
};

export function listSpendByCategory(params: {
  month?: string;
  year?: string;
  label_id?: number;
}): Promise<SpendByCategoryResponse> {
  const qs = new URLSearchParams();
  if (params.month) qs.set("month", params.month);
  if (params.year) qs.set("year", params.year);
  if (params.label_id != null) qs.set("label_id", String(params.label_id));
  return request<SpendByCategoryResponse>(
    `/dashboards/spend-by-category?${qs}`,
  );
}

/** One row of the spend-by-tag breakdown (PRD §F3a; tag-analysis arc Phase B).
 * `total_paise` is signed (spend negative, refund positive — nets within a tag,
 * like SpendByCategoryRow). Both ids are null for the untagged bucket (txns with
 * zero labels), pinned last. NOTE: per-tag rows DOUBLE-COUNT a multi-tagged txn
 * across its tags (tags are many:many), so Σ(rows) overshoots `total_spend_paise`
 * — intended, never "fixed". */
export type SpendByTagRow = {
  label_id: number | null;
  label_name: string | null;
  total_paise: number;
};

/** Response body of `GET /dashboards/spend-by-tag`. `rows` are most-negative
 * first with the untagged bucket pinned last. Coverage (measured by amount, the
 * honesty guardrail): `total_spend_paise` is the HONEST signed total (no
 * double-count — not Σ(rows)); `tagged_paise` = total − untagged counts each
 * tagged txn once; `coverage_rate` is the signed ratio when it lands in [0,1],
 * else null (zero-spend month, or refund-skew pushes it out of range → render
 * "—" + raw amounts). Signed figures are never clamped. */
export type SpendByTagResponse = {
  period: string;
  rows: SpendByTagRow[];
  total_spend_paise: number;
  tagged_paise: number;
  coverage_rate: number | null;
};

export function listSpendByTag(params: {
  month?: string;
  year?: string;
}): Promise<SpendByTagResponse> {
  const qs = new URLSearchParams();
  if (params.month) qs.set("month", params.month);
  if (params.year) qs.set("year", params.year);
  return request<SpendByTagResponse>(`/dashboards/spend-by-tag?${qs}`);
}

/** One row of the top-merchants list (PRD §F8 view 3). `total_paise` is SIGNED
 * (spend negative, refund positive — nets like SpendByCategoryRow), so a
 * refund-dominant merchant surfaces positive. `merchant_label` is a representative
 * raw display string; `merchant_normalized` is the grouping key. */
export type TopMerchantRow = {
  merchant_normalized: string;
  merchant_label: string;
  total_paise: number;
};

/** Response body of `GET /dashboards/top-merchants`. `rows` is ordered
 * most-negative first (biggest spender first) and capped at `limit`; the
 * no-merchant bucket is excluded. `total_merchants` is the distinct non-empty
 * merchant count (pre-LIMIT) for the "top N of M" label; `truncated` =
 * total_merchants > limit. */
export type TopMerchantsResponse = {
  period: string;
  rows: TopMerchantRow[];
  total_merchants: number;
  truncated: boolean;
  label_id: number | null;
};

export function listTopMerchants(params: {
  month?: string;
  year?: string;
  limit?: number;
  label_id?: number;
}): Promise<TopMerchantsResponse> {
  const qs = new URLSearchParams();
  if (params.month) qs.set("month", params.month);
  if (params.year) qs.set("year", params.year);
  // Omit when absent → server default (5). Sending it only when set keeps the
  // URL clean and avoids re-stating the default in two places.
  if (params.limit != null) qs.set("limit", String(params.limit));
  if (params.label_id != null) qs.set("label_id", String(params.label_id));
  return request<TopMerchantsResponse>(`/dashboards/top-merchants?${qs}`);
}

/** Income vs spend over [start, end] (the /expenses summary strip's income
 * figure). `expense_paise` is signed (≤ 0 typically — refunds net against
 * spend, matching SpendByPeriodBucket); `income_paise` ≥ 0; `net_paise` =
 * income + expense (server-computed). Transfers excluded. */
export type PeriodTotalsResponse = {
  start: string;
  end: string;
  income_paise: number;
  expense_paise: number;
  net_paise: number;
};

export function listPeriodTotals(params: {
  start: string;
  end: string;
}): Promise<PeriodTotalsResponse> {
  const qs = new URLSearchParams({ start: params.start, end: params.end });
  return request<PeriodTotalsResponse>(`/dashboards/period-totals?${qs}`);
}

/** F3 auto-tag acceptance metric (PRD §Success-metrics). Of board rows the
 * import auto-tagged, the fraction whose final category still equals the
 * suggestion. `acceptance_rate` is null when `total_auto_tagged === 0`
 * ("no data" — render "—", not "0%").
 *
 * `coverage_rate` is a DISTINCT metric, not a replacement: of ALL imported
 * board rows (`imported_total`), the fraction that got a category suggestion
 * at all (`pre_tagged`) — PRD §Success-metrics' ≥80% pre-tag bar, which
 * `acceptance_rate` has no denominator to measure. Null when
 * `imported_total === 0`, same "no data" contract. */
export type TaggingStatsResponse = {
  total_auto_tagged: number;
  kept: number;
  acceptance_rate: number | null;
  rules_count: number;
  imported_total: number;
  pre_tagged: number;
  coverage_rate: number | null;
};

export function getTaggingStats(): Promise<TaggingStatsResponse> {
  return request<TaggingStatsResponse>("/dashboards/tagging-stats");
}

/** One account's current balance for the Overview accounts panel (PRD §F8).
 * `balance_paise` is signed: opening balance + the board-only signed sum of the
 * account's transactions. A credit card is treated as a *spend channel*, not a
 * liability (bill payments aren't recorded) — its `balance_paise` is lifetime
 * accumulated spend, so the UI shows `spend_ytd_paise` instead and net worth
 * excludes credit cards. An `investment` account is excluded too, for the
 * opposite reason: it's a placeholder whose balance is pinned to 0 at create,
 * because the money it would hold is already counted as holdings. This row still
 * reports the raw signed balance for every type, excluded or not.
 * `spend_ytd_paise` is the signed net Σ(spend, signed) — spend rows of either
 * sign, refunds netting in as the positive ones — over the calendar
 * year-to-date window, populated only for `credit_card` rows
 * (`null` otherwise, never `0`); it may be positive in a refund-dominant window,
 * so display floors it via `max(0, −value)`. Archived accounts are included
 * (flagged) so net worth doesn't change on archive. */
export type AccountBalanceRow = {
  account_id: number;
  name: string;
  type: AccountRead["type"];
  currency: AccountRead["currency"];
  balance_paise: number;
  spend_ytd_paise: number | null;
  gross_spend_ytd_paise?: number | null;
  refund_ytd_paise?: number | null;
  cashback_ytd_paise?: number | null;
  archived: boolean;
};

/** Response body of `GET /dashboards/overview` — the Financial Overview home
 * (PRD §F8 view 1 + view 4). `net_worth_paise` = Σ contributing account balances +
 * portfolio value (server-computed), where contributing means `bank` and `cash`
 * only: credit cards are excluded (spend channels, not liabilities) and investment
 * accounts are excluded (placeholders — their value is already the portfolio term,
 * so counting both double-counts it). `portfolio_value_paise` skips null-NAV holdings (they
 * count as 0). `income_paise` / `expense_paise` / `net_paise` mirror
 * PeriodTotalsResponse for the requested period (`expense_paise` ≤ 0; net = income
 * + expense). Account balances are all-time; only the income/expense/net figures
 * are period-scoped. `period` echoes the requested month or year verbatim.
 * `fx_unavailable_count` is an honesty flag: priced USD holdings left out of the
 * rollup because no FX rate is cached, so net worth never silently shrinks —
 * render it, don't drop it. */
export type OverviewResponse = {
  period: string;
  net_worth_paise: number;
  portfolio_value_paise: number;
  fx_unavailable_count: number;
  income_paise: number;
  expense_paise: number;
  net_paise: number;
  accounts: AccountBalanceRow[];
};

export function getOverview(params: {
  month?: string;
  year?: string;
}): Promise<OverviewResponse> {
  const qs = new URLSearchParams();
  if (params.month) qs.set("month", params.month);
  if (params.year) qs.set("year", params.year);
  return request<OverviewResponse>(`/dashboards/overview?${qs}`);
}

// --- Investments (PRD §F7) ----------------------------------------------------
// Money fields stay integer minor units (`*_native_paise`, like amount_paise);
// quantity / price / NAV / fx-rate arrive as exact **decimal strings** (the
// backend serializes Decimals as strings to dodge JS float). Render them via
// format.ts's formatUnits / formatDecimalMoney; never round-trip through Number.
export type AssetClass =
  | "indian_equity"
  | "indian_mf"
  | "us_equity"
  | "us_etf"
  | "fd"
  | "bond"
  | "nps"
  | "gold"
  | "other";
export type Exchange =
  "NSE" | "BSE" | "MFCentral" | "NASDAQ" | "NYSE" | "OTHER";
export type InvestmentTransactionType =
  | "buy"
  | "sell"
  | "sip"
  | "dividend"
  | "bonus"
  | "split"
  | "switch_in"
  | "switch_out";

export type InstrumentRead = {
  id: number;
  symbol: string;
  name: string;
  asset_class: AssetClass;
  currency: AccountRead["currency"];
  exchange: Exchange;
  /** 12-char ISO 6166. Write-once (fill-if-null on create, 422 on a conflicting PATCH)
   * and the sole key AMFI NAVAll is matched on — an `indian_mf` without one can never be
   * auto-priced. Read here so a form can show whether it is already set. */
  isin: string | null;
  current_nav: string | null; // decimal string, native ccy
  /** The date `current_nav` is VALID FOR, not when it was written — the auto snapshot's
   * source NAV date, or the client's `nav_as_of`. Naive UTC midnight for a date-sourced
   * stamp, so it serializes with no offset suffix. */
  nav_updated_at: string | null;
  archived_at: string | null;
};

export type InstrumentCreate = {
  symbol: string;
  name: string;
  asset_class: AssetClass;
  currency?: AccountRead["currency"]; // derived from asset class (US → USD); defaults INR
  exchange: Exchange;
  /** 12-char ISO 6166, normalised server-side (trimmed + upper-cased). Without one an
   * `indian_mf` can never be matched to AMFI NAVAll and stays hand-priced forever. */
  isin?: string | null;
  current_nav?: string | null;
  /** The date `current_nav` is VALID FOR (YYYY-MM-DD), not when it was typed. Defaults
   * server-side to today. Only meaningful alongside a `current_nav`: sending one without,
   * or a date in the future, is a 422. */
  nav_as_of?: string | null;
};

export type InvestmentTransactionRead = {
  id: number;
  instrument_id: number;
  date: string;
  transaction_type: InvestmentTransactionType;
  units: string; // decimal string ("0" for dividend)
  price_per_unit_native: string | null;
  amount_native_paise: number;
  fees_native_paise: number;
  fx_rate_to_inr: string;
  note: string | null;
  /** The other leg of ONE economic event, when this row is half of a pair — today
   * only an IDCW reinvestment (a `dividend` + `buy` couple). Server-managed and
   * read-only: neither Create body accepts it. Without it a flat list renders a
   * reinvestment as two unrelated same-date events. */
  pair_id: number | null;
};

export type InvestmentTransactionCreate = {
  date: string;
  instrument_id: number;
  transaction_type: InvestmentTransactionType;
  units: string;
  price_per_unit_native?: string | null;
  amount_native_paise: number;
  fees_native_paise?: number;
  note?: string | null;
};

/** An Indian MF IDCW **reinvestment**: `amount_native_paise` of dividend became
 * `units` at NAV `price_per_unit_native` on `date`. One call, two linked rows.
 *
 * Deliberately has no `fees_native_paise` and no `transaction_type` — a reinvestment
 * carries no brokerage, and its two legs' types are fixed server-side. The backend
 * schema is `extra="forbid"`, so sending either fails loudly rather than silently
 * capitalising a fee into the lot. */
export type ReinvestmentCreate = {
  date: string;
  instrument_id: number;
  amount_native_paise: number;
  units: string;
  price_per_unit_native: string;
  note?: string | null;
};

/** The two legs of a recorded reinvestment, named rather than positional — the client
 * never has to infer which is which from `transaction_type`. */
export type ReinvestmentRead = {
  dividend: InvestmentTransactionRead;
  buy: InvestmentTransactionRead;
};

/** One current position (computed FIFO read-model). `*_native_paise` is the
 * instrument's own currency (per-row display); `*_inr_paise` is the home-currency
 * rollup — use it for any cross-holding aggregation (e.g. %Alloc) so INR and USD
 * rows don't get summed 1:1. net_units / avg_cost_native / current_nav are decimal
 * strings. Value + pnl are null when the instrument has no NAV; the INR value/pnl
 * are additionally null for a USD holding with no cached FX rate. */
export type HoldingRead = {
  instrument_id: number;
  symbol: string;
  name: string;
  asset_class: AssetClass;
  currency: AccountRead["currency"];
  net_units: string;
  avg_cost_native: string;
  invested_native_paise: number;
  current_nav: string | null;
  current_value_native_paise: number | null;
  unrealized_pnl_native_paise: number | null;
  invested_inr_paise: number;
  current_value_inr_paise: number | null;
  unrealized_pnl_inr_paise: number | null;
  /** How old the price behind `current_value` is, in calendar days; null when the
   * holding has no NAV. Server-computed on purpose — `nav_updated_at` serializes without
   * a timezone suffix, which `new Date()` would read as local time. Compare against
   * `STALENESS_WARN_DAYS`; never re-derive it. */
  nav_staleness_days: number | null;
};

export type HoldingsResponse = { holdings: HoldingRead[] };

export function listInstruments(): Promise<InstrumentRead[]> {
  return request<InstrumentRead[]>("/instruments");
}

export function createInstrument(
  body: InstrumentCreate,
): Promise<InstrumentRead> {
  return request<InstrumentRead>("/instruments", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// Instrument PATCH (NAV edit) and instrument/txn DELETE have backend routes but
// no UI consumer this slice — add the client fns when the edit/delete surface
// lands (CLAUDE.md §2: no client export without a call site to type-check it).

/** List investment transactions, newest-first (server default limit 50). No
 * filter params wired this slice — the board lists all (CLAUDE.md §2). */
export function listInvestmentTransactions(): Promise<
  InvestmentTransactionRead[]
> {
  return request<InvestmentTransactionRead[]>("/investment-transactions");
}

export function createInvestmentTransaction(
  body: InvestmentTransactionCreate,
): Promise<InvestmentTransactionRead> {
  return request<InvestmentTransactionRead>("/investment-transactions", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Record an IDCW reinvestment as ONE atomic call writing both legs, so the pair
 * invariant stays server-enforced and `pair_id` never becomes client-settable.
 * Corrections are DELETE + re-create, not PATCH (editing units would rewrite the
 * FIFO history a holding's cost basis depends on). */
export function createReinvestment(
  body: ReinvestmentCreate,
): Promise<ReinvestmentRead> {
  return request<ReinvestmentRead>("/investment-transactions/reinvestment", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function listHoldings(): Promise<HoldingsResponse> {
  return request<HoldingsResponse>("/holdings");
}

// --- Portfolio summary (PRD §F8 view 6 / §F9) ---------------------------------
/** One asset-class slice of the allocation donut. `value_paise` is over
 * NAV-bearing holdings only (null-NAV excluded); the client derives each slice's
 * share as value_paise / Σ value_paise. */
export type AssetClassAllocation = {
  asset_class: AssetClass;
  value_paise: number;
};

/** Per-holding money-weighted return, keyed by instrument_id for a client-side
 * merge into the holdings table. `xirr` is an annualized fraction (0.12 = 12%),
 * null when unsolvable (single cashflow, all-same-sign, or null-NAV). */
export type HoldingXirr = {
  instrument_id: number;
  xirr: number | null;
};

/** Response of `GET /portfolio/summary` (PRD §F8 view 6). value / invested /
 * unrealized are over NAV-bearing holdings only (null-NAV count ₹0). `xirr` is
 * the portfolio-wide annualized fraction, null when unsolvable.
 * `fx_unavailable_count` tallies holdings that *are* priced but have no cached FX
 * rate — excluded from the totals and the XIRR, so it must be surfaced. */
export type PortfolioSummaryResponse = {
  current_value_paise: number;
  invested_paise: number;
  unrealized_pnl_paise: number;
  xirr: number | null;
  holdings_count: number;
  null_nav_count: number;
  fx_unavailable_count: number;
  allocations: AssetClassAllocation[];
  holding_xirr: HoldingXirr[];
};

export function getPortfolioSummary(): Promise<PortfolioSummaryResponse> {
  return request<PortfolioSummaryResponse>("/portfolio/summary");
}

/** One curated index fund the portfolio can be measured against (GET /benchmarks). */
export type Benchmark = {
  id: number;
  name: string;
  kind: "index_fund";
  amfi_code: string;
  currency: "INR" | "USD";
  inception_date: string | null;
};

export function listBenchmarks(): Promise<Benchmark[]> {
  return request<Benchmark[]>("/benchmarks");
}

/** Why `benchmark_xirr` is null — the frontend switches on these (mirrors the
 * backend `BenchmarkUnavailableReason` Literal). */
export type BenchmarkUnavailableReason =
  | "no_benchmark_data"
  | "no_portfolio_cashflows"
  | "as_of_before_inception"
  | "negative_units"
  | "zero_terminal"
  | "unsolved";

/** Response of `GET /portfolio/performance` (PRD §F8 view 5) — the scalar alpha.
 * XIRRs / alpha are annualized fractions (0.12 = 12%; alpha in fraction-points),
 * null when unsolvable. Money is INR paise. The flags are honesty signals: the
 * benchmark is a post-expense fund (not the raw index); `partial` = history gap;
 * `benchmark_cache_stale` = a cashflow/terminal fell past the cached NAVs;
 * `is_multi_asset` = portfolio spans classes the single index doesn't; staleness
 * surfaces a lagging portfolio valuation rather than fabricating alpha.
 * `fx_staleness_days` is the FX twin of `nav_staleness_days` (a USD holding priced
 * off a weeks-old rate is computable but worth flagging) and `fx_unavailable_count`
 * counts priced USD holdings excluded outright for want of any cached rate. */
export type PortfolioPerformanceResponse = {
  benchmark_id: number;
  benchmark_name: string;
  is_fund_post_ter: boolean;
  portfolio_xirr: number | null;
  benchmark_xirr: number | null;
  alpha: number | null;
  portfolio_value_paise: number;
  benchmark_value_paise: number;
  value_gap_paise: number;
  partial: boolean;
  benchmark_cache_stale: boolean;
  is_multi_asset: boolean;
  nav_staleness_days: number | null;
  null_nav_count: number;
  fx_staleness_days: number | null;
  fx_unavailable_count: number;
  benchmark_unavailable_reason: BenchmarkUnavailableReason | null;
};

export function getPortfolioPerformance(
  benchmarkId?: number,
): Promise<PortfolioPerformanceResponse> {
  const q = benchmarkId != null ? `?benchmark_id=${benchmarkId}` : "";
  return request<PortfolioPerformanceResponse>(`/portfolio/performance${q}`);
}

// --- Price refresh (PRD §F7 / §F9) --------------------------------------------
// Two explicit, cold-triggered backfills behind the UI sync button. Both make
// sequential external HTTP calls server-side and degrade gracefully — a slow or
// failed source becomes a counted error, never a 500. The counts ARE consumed:
// `RefreshPricesButton` reads warnings.length, fetch_errors, mf_updated +
// equity_updated and benchmarks_refreshed to report what changed. So a count added
// backend-side needs a renderer here — adding one is not caught by tsc (a field the
// TS type never declares produces no diagnostic), unlike removing one.

/** Response of `POST /instruments/refresh-navs` — refreshes the user's holdings'
 * NAVs (AMFI for MFs, Yahoo for equities). `catalogue_staleness_days` is the oldest
 * valuation across every ACTIVE PRICED INSTRUMENT — exited positions included, so it is
 * NOT `PortfolioPerformanceResponse.nav_staleness_days` (the held set) and the two can
 * differ by hundreds of days for one user. Treat it as catalogue hygiene, not a user
 * warning. `warnings` are PII-safe: instrument ids plus public
 * reference data (AMFI scheme names / ISINs, Yahoo tickers) — never a merchant, amount,
 * account number or card last-4. Renderable verbatim. */
export type NavRefreshSummary = {
  mf_updated: number;
  equity_updated: number;
  unmatched: number;
  fetch_errors: number;
  stale_skipped: number;
  skipped: number;
  null_nav_count: number;
  catalogue_staleness_days: number | null;
  warnings: string[];
};

/** Response of `POST /benchmarks/refresh` — backfills the benchmark NAV cache
 * from mfapi (idempotent; a same-day re-run inserts 0). */
export type BenchmarkRefreshSummary = {
  benchmarks_refreshed: number;
  navs_inserted: number;
  fetch_errors: number;
  warnings: string[];
};

export function refreshInstrumentNavs(): Promise<NavRefreshSummary> {
  return request<NavRefreshSummary>("/instruments/refresh-navs", {
    method: "POST",
  });
}

export function refreshBenchmarks(): Promise<BenchmarkRefreshSummary> {
  return request<BenchmarkRefreshSummary>("/benchmarks/refresh", {
    method: "POST",
  });
}

// --- Hierarchical Dashboards --------------------------------------------------

export type HierarchicalSubcategorySpend = {
  category_id: number | null;
  category_name: string;
  spend_paise: number;
  total_paise: number;
  percentage: number;
  is_direct?: boolean;
  color?: string | null;
};

export type HierarchicalParentSpend = {
  parent_id: number | null;
  parent_name: string;
  spend_paise: number;
  direct_paise: number;
  total_paise: number;
  percentage: number;
  color?: string | null;
  subcategories: HierarchicalSubcategorySpend[];
};

export type SubcategoryMover = {
  category_id: number | null;
  category_name: string;
  parent_name?: string | null;
  current_paise: number;
  previous_paise: number;
  delta_paise: number;
  growth_rate: number | null;
};

export type HierarchicalSpendResponse = {
  period: string;
  total_spend_paise: number;
  parents: HierarchicalParentSpend[];
  top_movers: SubcategoryMover[];
  label_id?: number | null;
};

export function getHierarchicalSpend(params: {
  month?: string;
  year?: string;
  label_id?: number;
}): Promise<HierarchicalSpendResponse> {
  const qs = new URLSearchParams();
  if (params.month) qs.set("month", params.month);
  if (params.year) qs.set("year", params.year);
  if (params.label_id != null) qs.set("label_id", String(params.label_id));
  return request<HierarchicalSpendResponse>(
    `/dashboards/hierarchical-spend?${qs.toString()}`,
  );
}

export type HierarchicalSubcategoryRef = {
  category_id: number | null;
  category_name: string;
};

export type HierarchicalParentRef = {
  parent_id: number | null;
  parent_name: string;
  color?: string | null;
  subcategories: HierarchicalSubcategoryRef[];
};

export type HierarchicalSubcategoryPeriodTotal = {
  category_id: number | null;
  total_paise: number;
};

export type HierarchicalParentPeriodTotal = {
  parent_id: number | null;
  total_paise: number;
  subcategories: HierarchicalSubcategoryPeriodTotal[];
};

export type HierarchicalTrendBucket = {
  period: string;
  totals: HierarchicalParentPeriodTotal[];
};

export type HierarchicalTrendResponse = {
  bucket: "week" | "month";
  start: string;
  end: string;
  parents: HierarchicalParentRef[];
  buckets: HierarchicalTrendBucket[];
  label_id?: number | null;
};

export function getHierarchicalTrend(params: {
  bucket: "week" | "month";
  start: string;
  end: string;
  label_id?: number;
}): Promise<HierarchicalTrendResponse> {
  const qs = new URLSearchParams({
    bucket: params.bucket,
    start: params.start,
    end: params.end,
  });
  if (params.label_id != null) qs.set("label_id", String(params.label_id));
  return request<HierarchicalTrendResponse>(
    `/dashboards/hierarchical-trend?${qs.toString()}`,
  );
}
