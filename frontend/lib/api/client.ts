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
   * "Linked CC bill payment" banner (F4a auto-link or the manual link, ADR-0002 rule 11b), and freezes the row's identity fields
   * and type until `unlinkTransaction` breaks the pair (ADR-0007 rule 7). */
  transfer_pair_id: number | null;
  /** The spend this refund reverses (ADR-0009 rule 13), or null. Informational and
   * never summed. Read by the dialog's refund picker; written only through
   * `setRefundLink` / `deleteRefundLink`, never PATCH. */
  refund_of_id: number | null;
  /** Server-computed (ADR-0002 rule 11b placement): an unpaired, confirmed `income` or
   * `transfer` credit on a live card. The dialog shows "Paid your card bill elsewhere?"
   * from it; never re-derived here. */
  card_bill_link_offered: boolean;
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
  /** Row names a card-bill payment (PRD §F4a-1): a card `income` credit, or a
   * bank debit (negative `spend`) — the review queue can offer "Card bill
   * payment" instead of a category. Server-computed, never re-derived here.
   * Does NOT assert the auto-link will fire at commit; that also needs the
   * other leg (the bank debit, or the card's payment) and an imported parent
   * account. */
  cc_payment_candidate: boolean;
  /** This row, committed with no category, commits as an unpaired `transfer` (ADR-0002 rule
   * 7b): a flagged bank debit on a bank a live card names "Paid from", dated inside a committed
   * statement of that card, or a flagged card payment credit (rule 7c). Ignores the row's
   * current category. False on every other row.
   * Server-computed, never re-derived here. */
  uncategorised_commits_as_transfer: boolean;
};

/** Response body of `POST /imports`. `already_imported` means the file hash was
 * seen before (not that the upload was a no-op — a re-upload reconciles and can
 * re-stage rows missing from expenses). `pending_count` is the batch's rows still
 * awaiting review; route on it (>0 → review queue, 0 → nothing to review).
 * `duplicate_of_account_id` is *an* OTHER account this exact file was already
 * imported into (the wrong-account mis-import the per-account dedup can't catch) —
 * an id, never a name; `duplicate_of_account_archived` flags that `GET /accounts`
 * won't return it, so it can't be resolved to a label.
 * `skipped` = already in expenses (duplicates); `zero_amount_count` = zero-amount
 * lines, not transactions.
 * `reconciliation_delta_paise` (PRD §F1/§F4a) is this import's statement-balance
 * check as of upload: `null` = not checked (no usable statement metadata),
 * `0` = reconciled, non-zero = mismatch, this many paise. */
export type ImportSummary = {
  batch_id: number;
  imported: number;
  skipped: number;
  zero_amount_count: number;
  already_imported: boolean;
  pending_count: number;
  duplicate_of_account_id: number | null;
  duplicate_of_account_archived: boolean;
  reconciliation_delta_paise: number | null;
};

/** Response body of `POST /imports/investments` (PRD §F7). `warnings` are PII-safe
 * (line number + reason; never raw cells). `already_imported` flags an identical
 * re-upload (matched by source_file_hash) — nothing new was added. `rows_rejected`
 * counts rows; `warnings` may be fewer (one per uncovered sell of a refused instrument). */
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
  /** Whether an uploaded statement for this account has a parser. Server-derived
   * from the parser registry (the lookup an upload dispatches on) — never
   * re-derive it here by matching issuer names. */
  statement_import_supported: boolean;
};

/** One `(account type, issuer)` a statement parser exists for, with its display
 * label (`GET /accounts/statement-issuers`). The account forms render their issuer
 * pickers from this rather than a hand-mirrored list. */
export type StatementIssuer = {
  account_type: "credit_card" | "bank";
  issuer: string;
  label: string;
};

/** Spend categories serve `spend`-typed transactions, of either sign (a refund
 * is a positive `spend`); income categories serve `income`. Set at create,
 * immutable thereafter (the backend has no PATCH kind). */
export type CategoryKind = "spend" | "income";

/** A user-picked `#rrggbb` hex color for a category's dot/bar (the backend
 * validates and lower-cases it). `null` only on a subcategory, which inherits
 * its parent's hue; a root always has one (ADR-0012 rule 7). A plain string
 * alias: the value is an arbitrary hex, not a closed set. */
export type CategoryColor = string;

/** What a category is to the app, computed server-side (`category_roles`): the
 * kind's fallback "Other" (never archived or renamed, ADR-0012 rule 8) or the
 * spend-kind Cashback and its children (ADR-0013). The frontend branches on this
 * and never matches category names. */
export type CategoryRole = "fallback" | "cashback" | "investment_transfer";

export type CategoryRead = {
  id: number;
  name: string;
  kind: CategoryKind;
  is_seeded: boolean;
  archived_at: string | null;
  color: CategoryColor | null;
  /** Server-resolved: own colour or the parent's (ADR-0012 rule 7); never derive it. */
  display_color: CategoryColor;
  parent_id: number | null;
  role: CategoryRole | null;
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
export async function request<T>(
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
  /** Always on the wire (`AuthUser.is_guest` defaults false server-side). A guest owns
   * synthetic data only and cannot upload files (ADR-0003 rule 15). */
  is_guest: boolean;
  /** Server-computed (`auth_service.can_manage_credentials`): false for the shared demo
   * login and for guests. The change-password and delete-account cards render a notice
   * instead of their form when false; never re-derive it from `is_guest` or the email. */
  can_manage_credentials: boolean;
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

export function demoSession(): Promise<AuthUser> {
  return request<AuthUser>(
    "/auth/demo-session",
    { method: "POST" },
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

/** The typed confirmation phrase `POST /auth/delete-account` requires (ADR-0003 rule 13b).
 * Mirrors `DELETE_ACCOUNT_PHRASE` in `backend/app/schemas/auth.py`: exact, case-sensitive. */
export const DELETE_ACCOUNT_PHRASE = "delete my account" as const;
export type DeleteAccountRequest = { password: string; confirmation: typeof DELETE_ACCOUNT_PHRASE };

/** Erase the signed-in account and everything it owns, immediately (ADR-0003 rule 13b).
 * 204 clears the auth cookies. A wrong password is 400 (a body error, never 401, so it never
 * triggers a refresh or a logout); 403 for the demo login and guests; 422 for a wrong phrase.
 * Participates in refresh like `changePassword`. */
export function deleteMyAccount(body: DeleteAccountRequest): Promise<void> {
  return request<void>("/auth/delete-account", { method: "POST", body: JSON.stringify(body) });
}

/** Public, pre-auth client config. `demo_login_enabled` mirrors the backend's resolved
 * shared-demo-login gate and `guest_sandbox_enabled` its separate guest-sandbox gate
 * (`POST /auth/demo-session`); either one shows the "Try the demo" button, whose
 * `loginDemo` tries a guest sandbox first and falls back to the shared demo login.
 * `registration_enabled` reflects whether user registration is allowed.
 * `noRefresh`: this is read before any session exists, so a 401 must never trigger a
 * refresh attempt. */
export type AuthConfig = {
  demo_login_enabled: boolean;
  guest_sandbox_enabled: boolean;
  registration_enabled: boolean;
};

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

export function listStatementIssuers(): Promise<StatementIssuer[]> {
  return request<StatementIssuer[]>("/accounts/statement-issuers");
}

// Every call site today passes no params, so the shared `["categories"]`
// TanStack key (used at all 11 call sites) is safe — a future caller that
// passes `kind` (or calls `listCategoryTree`) must fold the params into its
// own query key (e.g. `["categories", params]`) or it will collide with the
// unfiltered cache entry.
/** The palette the server hands out and the colour picker offers, in slot order. */
export type CategoryPalette = { colors: CategoryColor[] };

export function getCategoryPalette(): Promise<CategoryPalette> {
  return request<CategoryPalette>("/categories/palette");
}

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
// `color` is omitted on create to inherit (subcategory) or have the server assign
// one (root); on update, `null` reverts a subcategory to inheriting and is a 422
// on a root.
export type CategoryCreate = {
  name: string;
  kind?: CategoryKind;
  color?: CategoryColor;
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

/** Soft-delete (archive) a category and its children (204). Its merchant→category rules
 * are kept but stop applying while it is archived. With `reassignTo`, every board row on
 * the archived categories moves to that live, same-kind category in the same commit
 * (ADR-0012 rule 10); an invalid target is a 422 before anything is written. Throws
 * `ApiError(404)` if already archived. */
export function deleteCategory(id: number, opts?: { reassignTo?: number }): Promise<void> {
  const query = opts?.reassignTo != null ? `?reassign_to=${opts.reassignTo}` : "";
  return request<void>(`/categories/${id}${query}`, { method: "DELETE" });
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

// One rule group per CANONICAL merchant (ADR-0011 rule 8): the merchant itself
// when unaliased. Named like `TopMerchantRow.merchant_canonical`.
export type MerchantRuleRead = {
  merchant_canonical: string;
  categories: CategoryRuleRead[];
  labels: LabelRuleRead[];
  alias_count: number; // alias patterns resolving to this canonical; 1 when unaliased
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

/** The most ids one bulk call (bulk-categorise, bulk-delete) accepts. A hand-kept mirror of
 * `services/bulk_categorise.py::BULK_MAX`; the server's 422 backstops any drift. */
export const BULK_MAX = 500;

export type BulkCategoriseResult = { updated: number; taught: number };

/** Categorise many board rows at once (ADR-0004 rule 4): all-or-nothing, one teach per
 * canonical merchant. Any bad id is a 422 whose body lists `invalid_ids` (see
 * `invalidIdsFromError`) and nothing is written. */
export function bulkCategorise(body: {
  transaction_ids: number[];
  category_id: number;
}): Promise<BulkCategoriseResult> {
  return request<BulkCategoriseResult>("/transactions/bulk-categorise", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Recent rows a category correction could also be applied to (ADR-0004 rules 2b and 4).
 * `transaction_ids` is empty (and `category_id` null) when there is nothing to offer. */
export type PastRowsRead = {
  merchant: string;
  category_id: number | null;
  transaction_ids: number[];
};

export function getPastRows(id: number): Promise<PastRowsRead> {
  return request<PastRowsRead>(`/transactions/${id}/past-rows`);
}

/** Pull `invalid_ids` from an all-or-nothing 422 body (bulk categorise, bulk delete, import commit);
 * `[]` for any other error shape. */
export function invalidIdsFromError(err: unknown): number[] {
  if (!(err instanceof ApiError) || err.status !== 422) return [];
  const detail = (err.body as { detail?: unknown } | undefined)?.detail;
  const ids = (detail as { invalid_ids?: unknown } | undefined)?.invalid_ids;
  return Array.isArray(ids)
    ? ids.filter((x): x is number => typeof x === "number")
    : [];
}

/** Hard-delete many rows in one all-or-nothing request (204). Each row goes through
 * the shared server delete: a linked partner outside the request gets its pre-link
 * type and category back (ADR-0002 rule 9), and refunds pointing at a deleted original
 * lose the link (ADR-0009 rule 13). A missing id is a 422 listing `invalid_ids`
 * (see `invalidIdsFromError`) and nothing is deleted. */
export function bulkDeleteTransactions(body: { transaction_ids: number[] }): Promise<void> {
  return request<void>("/transactions/bulk-delete", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Break a transfer pair (PRD §F4a-1 "break the link if the auto-detection got it
 * wrong", and the manual link of ADR-0002 rule 11b). Clears `transfer_pair_id` on BOTH
 * legs and gives each leg back what the link took: its `pre_link_type` and its
 * `pre_link_category_id` (ADR-0002 rule 9). A category set on the linked leg since then
 * wins. A leg born as a transfer (F2) has neither and stays `transfer`. Neither column is
 * on `TransactionRead`, so refetch the row to see the restored type and category.
 * Idempotent 204 on an already unpaired row. Unlinking is also the prerequisite for
 * editing a paired row's identity fields (ADR-0007 rule 7). */
export function unlinkTransaction(id: number): Promise<void> {
  return request<void>(`/transactions/${id}/unlink`, { method: "POST" });
}

/** The originals a refund may link to (ADR-0009 rule 13): server-ranked, at most 20, and
 * each one a row `setRefundLink` will accept. `[]` for a row that is not a refund. */
export function listRefundCandidates(id: number): Promise<TransactionRead[]> {
  return request<TransactionRead[]>(`/transactions/${id}/refund-candidates`);
}

/** Link a refund to the spend it reverses (204). Informational: nothing is re-summed. */
export function setRefundLink(id: number, originalId: number): Promise<void> {
  return request<void>(`/transactions/${id}/refund-link`, {
    method: "PUT",
    body: JSON.stringify({ original_id: originalId }),
  });
}

/** Remove a refund's link (204, idempotent). */
export function deleteRefundLink(id: number): Promise<void> {
  return request<void>(`/transactions/${id}/refund-link`, { method: "DELETE" });
}

/** Body of `POST /transactions/{id}/link` (mirrors `schemas/transactions.py::TransactionLinkSet`). */
export type TransactionLinkSet = { partner_id: number };

/** The rows `linkTransaction` will accept for this one (ADR-0002 rule 11b). The server vets
 * them with the same rule as the POST, puts the nearest date first and returns at most 20;
 * `[]` for a row that cannot be linked. Render as returned: never filter or re-sort. */
export function listLinkCandidates(id: number): Promise<TransactionRead[]> {
  return request<TransactionRead[]>(`/transactions/${id}/link-candidates`);
}

/** Manually pair two confirmed rows as a transfer ("Paid your card bill elsewhere?",
 * ADR-0002 rule 11b). 204. Both legs become `transfer`, keep their pre-link type and
 * category aside for unlink, and lose their category. 422 with the reason in `detail`
 * when the pair is refused; a missing or foreign partner is 422 too. Never teaches
 * (ADR-0004), so no rule cache moves. */
export function linkTransaction(id: number, partnerId: number): Promise<void> {
  const body: TransactionLinkSet = { partner_id: partnerId };
  return request<void>(`/transactions/${id}/link`, { method: "POST", body: JSON.stringify(body) });
}

/**
 * Manual transaction entry (PRD §F2). `amount_paise` is signed — the caller
 * applies the sign by entry direction (spend negative, income positive; a
 * refund is a `spend` with a positive amount and an income reversal an `income`
 * with a negative one — see `EntryDirection` in
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

/** A bundled synthetic sample statement a guest can import (ADR-0003 rule 15).
 * `account_ids` are the caller's live accounts it fits, decided server-side. */
export type SampleStatementRead = {
  id: string;
  label: string;
  issuer: string;
  account_type: "credit_card" | "bank";
  account_ids: number[];
};

export function listSampleStatements(): Promise<SampleStatementRead[]> {
  return request<SampleStatementRead[]>("/imports/samples");
}

/** Stage a bundled sample into one of the caller's accounts; same summary as an upload. */
export function importSampleStatement(body: {
  sample_id: string;
  account_id: number;
}): Promise<ImportSummary> {
  return request<ImportSummary>("/imports/samples", {
    method: "POST",
    body: JSON.stringify(body),
  });
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
  links_not_restored: number;
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

/** Commit `transaction_ids` and delete `discard_ids` in one atomic request (204;
 * ADR-0002 rule 5). Either list may be empty, not both. A row that is not a pending row
 * of this batch, or is named in both lists, is a 422 `{message, invalid_ids}`
 * (see `invalidIdsFromError`) and nothing is committed or deleted. */
export function commitImport(
  batchId: number,
  body: { transaction_ids: number[]; discard_ids: number[] },
): Promise<void> {
  return request<void>(`/imports/${batchId}/commit`, {
    method: "POST",
    body: JSON.stringify(body),
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
 * income-vs-spend chart). `period` is "YYYY-MM" or "YYYY-Www". `income_paise` is
 * signed (an income reversal nets it down);
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

/** One tag in the spend-by-tag-by-period echoed set (PRD §F3a labels, the
 * /spending tag-trend chart — arc Phase C): its id + name, defining the stable
 * line order and the palette-by-rank color join. Unlike CategoryTrendParent.parent_id, id and
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
 * uncategorized bucket. `parent_id` / `parent_name` name the category's parent
 * (one hop, ADR-0012 rule 6) whatever the archive state, so a rollup needs no
 * category list; both are null for a root and for the uncategorized bucket.
 * `display_color` / `parent_display_color` are the two rows' server-resolved colours
 * (ADR-0012 rule 7), whatever the archive state, so view 2 needs no category list for
 * colour; `display_color` is null only for the uncategorized bucket, and
 * `parent_display_color` for a root and for uncategorized. */
export type SpendByCategoryRow = {
  category_id: number | null;
  category_name: string | null;
  parent_id: number | null;
  parent_name: string | null;
  display_color: CategoryColor | null;
  parent_display_color: CategoryColor | null;
  total_paise: number;
};

/** Year numbers, newest first; the route defines the span (`listAvailableYears`
 * returns calendar years, `listTaxYears` FY start years). */
export type AvailableYearsResponse = {
  years: number[];
};

/** Calendar years for the spend pickers (PRD §F8): `2026` = 1 Jan - 31 Dec 2026, the
 * span the dashboards' `?year=2026` covers. */
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
 * refund-dominant merchant surfaces positive. `merchant_label` is the display string (a raw
 * descriptor for an unaliased group, else the canonical); `merchant_canonical` is the
 * grouping key, the per-user alias canonical (ADR-0011 rule 8). */
export type TopMerchantRow = {
  merchant_canonical: string;
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
 * spend, matching SpendByPeriodBucket); `income_paise` is signed (an income
 * reversal nets it down); `net_paise` =
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
 * year-to-date window (from 1 January), populated only for `credit_card` rows
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
  gross_spend_paise: number;
  fronted_peer_entries_paise: number;
  net_personal_spend_paise: number;
  fronted_card_usage_paise: number;
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

/** One row of `GET /instruments/asset-classes`: the currencies a class may carry (one for a
 * fixed class, both for the rest). The server is the only home of this rule. */
export type AssetClassCurrencies = {
  asset_class: AssetClass;
  currencies: AccountRead["currency"][];
  supports_price_feed: boolean;
};

export type InstrumentCreate = {
  symbol: string;
  name: string;
  asset_class: AssetClass;
  currency?: AccountRead["currency"]; // one of the class's currencies from `GET /instruments/asset-classes`
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
  account_id: number;
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
  linked_spend_txn_id: number | null;
};

export type InvestmentTransactionCreate = {
  date: string;
  instrument_id: number;
  account_id?: number | null;
  transaction_type: InvestmentTransactionType;
  units: string;
  price_per_unit_native?: string | null;
  amount_native_paise: number;
  fees_native_paise?: number;
  note?: string | null;
  linked_spend_txn_id?: number | null;
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
  price_pnl_inr_paise: number | null;
  fx_pnl_inr_paise: number | null;
  /** How old the price behind `current_value` is, in calendar days; null when the
   * holding has no NAV. Server-computed on purpose — `nav_updated_at` serializes without
   * a timezone suffix, which `new Date()` would read as local time. Compare against
   * `STALENESS_WARN_DAYS`; never re-derive it. */
  nav_staleness_days: number | null;
  /** Whether this holding's asset class supports an automated price feed source (AMFI or
   * Yahoo quote). When false, refresh-navs skips it and the user is the price source. */
  supports_price_feed: boolean;
};

export type HoldingsResponse = { holdings: HoldingRead[] };

export function listInstruments(): Promise<InstrumentRead[]> {
  return request<InstrumentRead[]>("/instruments");
}

export function listAssetClassCurrencies(): Promise<AssetClassCurrencies[]> {
  return request<AssetClassCurrencies[]>("/instruments/asset-classes");
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

export type ListInvestmentTransactionsParams = {
  limit?: number;
  offset?: number;
  instrument_id?: number;
  transaction_type?: string[];
  date_from?: string;
  date_to?: string;
};

/** List investment transactions, newest-first. Supports pagination and filters. */
export function listInvestmentTransactions(
  params: ListInvestmentTransactionsParams = {},
): Promise<InvestmentTransactionRead[]> {
  const qs = new URLSearchParams();
  if (params.limit != null) qs.set("limit", String(params.limit));
  if (params.offset != null) qs.set("offset", String(params.offset));
  if (params.instrument_id != null) qs.set("instrument_id", String(params.instrument_id));
  if (params.date_from) qs.set("date_from", params.date_from);
  if (params.date_to) qs.set("date_to", params.date_to);
  if (params.transaction_type) {
    for (const t of params.transaction_type) qs.append("transaction_type", t);
  }
  const s = qs.toString();
  return request<InvestmentTransactionRead[]>(
    `/investment-transactions${s ? `?${s}` : ""}`,
  );
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

export function linkInvestmentTransaction(
  id: number,
  spendTransactionId: number,
): Promise<InvestmentTransactionRead> {
  return request<InvestmentTransactionRead>(`/investment-transactions/${id}/link`, {
    method: "POST",
    body: JSON.stringify({ spend_transaction_id: spendTransactionId }),
  });
}

export function unlinkInvestmentTransaction(
  id: number,
): Promise<InvestmentTransactionRead> {
  return request<InvestmentTransactionRead>(`/investment-transactions/${id}/unlink`, {
    method: "POST",
  });
}

export type ListHoldingsParams = {
  account_id?: number | null;
};

export function listHoldings(): Promise<HoldingsResponse>;
export function listHoldings(
  params: ListHoldingsParams,
): Promise<HoldingsResponse>;
export function listHoldings(
  params?: ListHoldingsParams,
): Promise<HoldingsResponse> {
  const qs = new URLSearchParams();
  if (params?.account_id != null) {
    qs.set("account_id", String(params.account_id));
  }
  const s = qs.toString();
  return request<HoldingsResponse>(`/holdings${s ? `?${s}` : ""}`);
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
  rate?: number | null;
  return_type?: "xirr" | "hpr" | null;
  hpr?: number | null;
  duration_days?: number | null;
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
  price_pnl_paise: number;
  fx_pnl_paise: number;
  xirr: number | null;
  rate: number | null;
  return_type: "xirr" | "hpr" | null;
  hpr: number | null;
  duration_days: number | null;
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
  | "unsolved"
  | "duration_too_short";

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

/** One drill row under a parent in the category trend (PRD §F8 view 6).
 * `category_id` is the parent's own id on the direct row (`is_direct`).
 * `category_name` is null only for a foreign-user category, which the composite
 * same-user FK makes unreachable. `totals_paise` is signed, aligned to
 * `CategoryTrendResponse.periods`, never clamped. */
export type CategoryTrendSubcategory = {
  category_id: number;
  category_name: string | null;
  /** The series colour the server chose: the palette rotation for an inheriting
   * subcategory, the parent's own colour on the direct row (ADR-0012 rule 7). */
  color: CategoryColor;
  archived: boolean;
  is_direct: boolean;
  totals_paise: number[];
};

/** One parent's monthly series; `parent_id` null is uncategorized (no subs).
 * Archived children are already rolled in (ADR-0012 rule 6): Σ subs = parent. */
export type CategoryTrendParent = {
  parent_id: number | null;
  parent_name: string | null;
  /** The parent's `display_color`; null only for uncategorized. */
  color: CategoryColor | null;
  archived: boolean;
  totals_paise: number[];
  subcategories: CategoryTrendSubcategory[];
};

/** Response body of `GET /dashboards/category-trend`. `periods` is `YYYY-MM`,
 * chronological. Every parent with an in-window row is returned; the chart
 * folds the tail. */
export type CategoryTrendResponse = {
  periods: string[];
  parents: CategoryTrendParent[];
  label_id: number | null;
};

export function getCategoryTrend(params: {
  months: number;
  label_id?: number;
}): Promise<CategoryTrendResponse> {
  const qs = new URLSearchParams({ months: String(params.months) });
  if (params.label_id != null) qs.set("label_id", String(params.label_id));
  return request<CategoryTrendResponse>(`/dashboards/category-trend?${qs}`);
}

// --- Tax statement (PRD §F11) ------------------------------------------------
/** One intraday match (ADR-0015 rules 7-8), mirroring `schemas/tax.py`. INR paise
 * (indian_* instruments are INR). `profit_paise` is server-computed; render as received. */
export type IntradayTradeRead = {
  sell_txn_id: number;
  buy_txn_id: number;
  instrument_id: number;
  symbol: string;
  name: string;
  date: string;
  units: string;
  buy_value_paise: number;
  sell_value_paise: number;
  sell_charges_paise: number;
  profit_paise: number;
};

export type IntradayTradesResponse = {
  period: string;
  trades: IntradayTradeRead[];
  total_profit_paise: number;
};

export function listIntradayTrades(year: number): Promise<IntradayTradesResponse> {
  return request<IntradayTradesResponse>(`/tax/intraday-trades?year=${year}`);
}

/** Tax years worth offering (FY start years, newest first): every year with a realised
 * sale, plus the current one. */
export function listTaxYears(): Promise<AvailableYearsResponse> {
  return request<AvailableYearsResponse>("/tax/available-years");
}

// --- Friends & Khata / Peer ledger (PRD §F3b, ADR-0017) -----------------------

export type PeerDirection = "you_gave" | "you_got";

export type PeerContact = {
  id: number;
  name: string;
  phone: string | null;
  upi_id: string | null;
  notes: string | null;
  archived_at: string | null;
  created_at: string;
};

export type PeerContactWithBalance = PeerContact & {
  balance_paise: number;
  total_gave_paise: number;
  total_got_paise: number;
  active_entries_count: number;
};

export type PeerEntry = {
  id: number;
  contact_id: number;
  transaction_id: number | null;
  date: string;
  amount_paise: number;
  direction: PeerDirection;
  is_settlement: boolean;
  notes: string | null;
  created_at: string;
};

export type PeerLedgerSummary = {
  total_receivable_paise: number;
  total_payable_paise: number;
  net_balance_paise: number;
  active_contacts_count: number;
};

export type PeerContactCreatePayload = {
  name: string;
  phone?: string | null;
  upi_id?: string | null;
  notes?: string | null;
};

export type PeerContactUpdatePayload = {
  name?: string;
  phone?: string | null;
  upi_id?: string | null;
  notes?: string | null;
};

export type PeerEntryCreatePayload = {
  contact_id: number;
  transaction_id?: number | null;
  date?: string;
  amount_paise: number;
  direction?: PeerDirection;
  is_settlement?: boolean;
  notes?: string | null;
  original_paise?: number;
  discount_paise?: number;
};

export type PeerSettlePayload = {
  date?: string;
  notes?: string | null;
};

export function listPeerContacts(
  includeArchived = false,
): Promise<PeerContactWithBalance[]> {
  const qs = new URLSearchParams();
  if (includeArchived) qs.set("include_archived", "true");
  const q = qs.toString();
  return request<PeerContactWithBalance[]>(`/friends/contacts${q ? `?${q}` : ""}`);
}

export function getPeerContact(id: number): Promise<PeerContactWithBalance> {
  return request<PeerContactWithBalance>(`/friends/contacts/${id}`);
}

export function createPeerContact(
  payload: PeerContactCreatePayload,
): Promise<PeerContact> {
  return request<PeerContact>("/friends/contacts", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updatePeerContact(
  id: number,
  payload: PeerContactUpdatePayload,
): Promise<PeerContact> {
  return request<PeerContact>(`/friends/contacts/${id}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function archivePeerContact(
  id: number,
  confirmWithBalance = false,
): Promise<PeerContact> {
  const qs = new URLSearchParams();
  if (confirmWithBalance) qs.set("confirm_with_balance", "true");
  const q = qs.toString();
  return request<PeerContact>(`/friends/contacts/${id}${q ? `?${q}` : ""}`, {
    method: "DELETE",
  });
}

export function settlePeerContact(
  id: number,
  payload?: PeerSettlePayload,
): Promise<PeerEntry> {
  return request<PeerEntry>(`/friends/contacts/${id}/settle`, {
    method: "POST",
    body: payload ? JSON.stringify(payload) : "{}",
  });
}

export function listPeerEntries(contactId: number): Promise<PeerEntry[]> {
  return request<PeerEntry[]>(`/friends/contacts/${contactId}/entries`);
}

export function createPeerEntry(
  payload: PeerEntryCreatePayload,
): Promise<PeerEntry> {
  return request<PeerEntry>("/friends/entries", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function deletePeerEntry(id: number): Promise<void> {
  return request<void>(`/friends/entries/${id}`, {
    method: "DELETE",
  });
}

export function getPeerSummary(): Promise<PeerLedgerSummary> {
  return request<PeerLedgerSummary>("/friends/summary");
}

export async function exportPeerContactStatement(id: number): Promise<{
  csvText: string;
  filename: string;
}> {
  const res = await sendWithRefresh(`/friends/contacts/${id}/export`);
  if (!res.ok) throw await toApiError(res);
  const disposition = res.headers.get("content-disposition") ?? "";
  const match = disposition.match(/filename="?([^"]+)"?/);
  return {
    csvText: await res.text(),
    filename: match?.[1] ?? `peer_contact_${id}_statement.csv`,
  };
}
