# Finance Tracker — Product Requirements Document

## Context

A personal finance tracker for a single user (you) that consolidates spending across
multiple credit cards, bank accounts, and cash, plus tracks investment portfolio with
proper return calculations. Built primarily because off-the-shelf options either don't
support Indian banks well, lack investment-side depth, or are SaaS products you don't
want your financial data sitting in. That last point is load-bearing: the app holds a
user's **full net-worth** picture — a higher-sensitivity target than ordinary PII — so
privacy is a first-class design constraint, and the deployment model that follows from it
(self-host for real data; a static synthetic-data demo for showcasing) is spelled out under
**Users & access**. v1 is local-first; the architecture leaves room to host it later
without rewrites.

## Goals

1. Import credit-card and bank statements (CSV + PDF) for Axis / ICICI (HDFC dropped
   from v1 — see §F1 Issuers), parse
   transactions, dedupe against existing rows, and tag them by category.
2. Learn your tagging habits — once you've categorised a merchant, future occurrences
   are auto-tagged.
3. Let you record manual transactions (cash spends, transfers, things imports missed).
4. Track investments at transaction level (buy / sell / SIP / dividend) and compute
   XIRR + allocation per holding. Covers:
   - **Indian mutual funds & equities** — imported from a broker / AMC transaction
     CSV (the canonical importer; one field set, broker-header aliases).
   - **US stocks & ETFs** held via INDmoney — imported from INDmoney's transaction
     export (CSV / PDF).
   - **Direct equity, FDs, NPS, gold, etc.** — entered manually.
5. Surface the data through dashboards: monthly/weekly spend bars, category breakdown,
   spend trend, net worth over time, portfolio composition. All multi-currency values
   are rolled up to INR (home currency) for top-line metrics.
6. Export the data as per-table CSV files and sync those to Google Drive on demand,
   so a copy of every transaction & holding lives outside this laptop.
7. Surface portfolio metrics live — current value, today's P&L, this-month P&L —
   updating without a manual refresh as data changes.

## Non-goals (v1)

- A **hosted instance that custodies real user data.** Real use means **self-hosting**
  (§Users & access); the hosted showcase is static / backend-less. *Public open signup was
  a v1 non-goal until 2026-08-08 and is now in scope* — `POST /auth/register` and
  `/register` ship it — but it opens on the self-hosted deployment, not on an operator-run
  one. Per-row multi-user scoping is **shipped** (email + password auth, every row scoped
  by `user_id` — see [ADR-0003](docs/adr/0003-multi-user-auth.md) and the auth row in
  §Users & access below); the hardening the reversal implies is recorded in ADR-0003
  §Session hardening, not built.
- Shared households — a cross-user roll-up (one view over two members' data) is v2.
  ADR-0003 deliberately leaves room for an additive household grouping; nothing in v1
  reads across users.
- OCR / scanned-bill imports — deferred to v2.
- Broker / MF Central / Account-Aggregator API integration — investment *transactions*
  are entered manually (CSV import or manual entry). NAV / price *quotes* are the one
  exemption: v1 auto-fetches Indian-MF NAVs (AMFI's public NAVAll file) and Indian-equity
  prices (a public quote source) — public price feeds, not account/holdings APIs — so this
  does not reopen broker integration.
- Multi-currency *spending*. INR only for credit-card / bank / cash transactions.
  Investment side does support **INR + USD** (because INDmoney holdings are USD); other
  currencies (GBP / EUR / etc.) are deferred to v2.
- Budgets, alerts, recurring-transaction detection — v2.
- Mobile apps. Web only, mobile-responsive layout.
- Restore-from-**Drive** UI, scheduled / automatic Drive sync, encrypted backups, and
  full-DB-dump exports — all v2. v1 ships manual CSV export plus an additive,
  non-destructive CSV *restore* (`POST /backup/import` + Settings → Backup); it is the
  Drive round-trip, not restore itself, that is out of v1. See §F10.

## Success metrics

This is a personal tool, not a SaaS — but without explicit success bars, scope
creeps and the project drifts. Targets for v1:

- **Adoption**: I stop using my current spreadsheet/notes within month 1 of v1
  shipping. Binary y/n.
- **Import-to-categorised latency**: median time from "downloaded CC statement PDF"
  to "all transactions appear with categories in the app" is **< 2 minutes**. This
  is the core daily-driver loop and the strongest signal that imports are usable.
- **Tagging precision**: after 3 months of usage, ≥80% of imported transactions
  arrive pre-tagged correctly (no edit needed). Signal that exact-match auto-tag
  (F3) is doing its job; below this, fuzzy/rules need to come forward. Carried by
  `coverage_rate` on `GET /dashboards/tagging-stats` (added alongside the merchant alias layer,
  [ADR-0011](docs/adr/0011-merchant-alias-layer.md)) — the fraction of imported rows that arrive
  pre-tagged, as distinct from `acceptance_rate` on the same endpoint, which answers a different
  question: of what we suggested, how much did the user keep?
- **XIRR accuracy**: portfolio-level XIRR within 0.1% of Kuvera / Groww's number
  for the same set of transactions. Signal that `pyxirr` + my data model are
  computing the right thing.
- **Maintenance budget**: the one upgrade weekend per year (see Maintenance posture)
  fits in a weekend. If it spills to a full week, the stack choice was wrong and
  needs revisiting.

## Users & access

**Deployment topology & data custody (the privacy model).** The app holds full
net-worth data, so *where it runs* decides who can see that data:

- **Self-host (the privacy path).** The whole stack — API + frontend + DB — runs on the
  user's own machine (`docker compose up`) or against their own free-tier DB. Compute
  *and* storage stay on their side; the operator holds nothing and sees nothing. This is
  what "local-first" means here — the stack is defined by `docker-compose.yml` plus the
  per-service Dockerfiles (`make up`; migrations self-apply on boot), with an opt-in
  single-origin reverse-proxy overlay for LAN/household access.
- **Hosted demo (the showcase path).** A **static, backend-less** frontend — the seeded
  synthetic dashboards baked into the build as JSON snapshots — deployed to a static host
  (Netlify / Vercel / Cloudflare Pages). No server, no DB, no uploads → no data custody, no
  DoS surface, no cross-visitor leakage, no cold start. Read-only: the interactive import
  pipeline (upload → parse → dedup → tag) can't run statically (the compute core is Python),
  so it is shown via an embedded **screencast**. The full live stack stays available as the
  self-host recipe (above) for anyone who wants to run the real thing.

The rule that forces this split — **a server can either compute over data or be blind to
it, not both.** Dashboards / XIRR / dedup all require reading *plaintext* transactions, so
any hosted instance that stores real data necessarily sees it. Encryption-at-rest only
protects a stolen disk, not a live compromise or the operator (the app must decrypt to
compute); true operator-blindness would require moving all compute client-side — a
zero-knowledge rewrite of the Python core, out of scope. Self-host sidesteps the whole
problem by never running an operator-controlled server.

**Decided: no hosted instance that custodies real data.** The hosted instance is
**demo-only** (synthetic data); anyone wanting real use **self-hosts** (their own machine or
their own free-tier DB). The operator therefore never custodies real net-worth data — the
whole privacy goal — and sidesteps the threat-model / breach-liability / defense-in-depth
burden that storing strangers' financial data would impose.

**Decided 2026-08-08: open self-service signup is in scope**, reversing the earlier "open
signup to store real data is out of scope". Signup opens on the **self-hosted** deployment —
which is where real data already lives — so it does not move custody to the operator; the
hosted demo is a static, backend-less site (above) with no server, so it cannot take a
registration at all. Auth's justification widens with it: it protects a self-hosted instance
that may be WAN-exposed and hold accounts for people the operator has never met, not only a
single user or a household of 2–3. The hardening that implies — an operator kill switch, the
register-409 enumeration oracle, email verification, and the per-worker rate limiter — is
recorded in [ADR-0003](docs/adr/0003-multi-user-auth.md) §Session hardening (amended
2026-08-08) and is **not built**.

**Household / family net worth (self-host, v2).** The concrete driver for multi-user beyond a
single login: a family self-hosts one instance, each member logs in and tracks their own
accounts, and the app rolls up a **combined family net worth**. This is the self-host privacy
model working as intended — the most sensitive aggregate (a household's total net worth) never
leaves the family's own box, and it needs **no** operator-hosted instance (members register on
the family's own box, or the household admin onboards them). Builds additively on the existing
`user_id` scoping: a `household` grouping + a cross-user aggregation layer. **Visibility = aggregate-only** (leaning): members see the
combined net worth and per-person totals, *not* each other's line-item transactions — that is
the "safely" requirement. (Full-transparency and opt-in-granular are the alternatives; pin the
model before building.) v2 — the auth foundation lands first; the roll-up rides on top.

**Auth phases (within the self-host / demo-only topology above):**

| Phase    | Access model                                                          |
| -------- | --------------------------------------------------------------------- |
| v1       | Single user, no login. App runs on `localhost`. `user_id = 1` hardcoded. |
| v1.5     | Add a single shared password / basic auth before any cloud deploy.    |
| v2 ✅ **shipped** | Real auth — email + password, httpOnly-cookie JWT + rotating server-revocable refresh; every row scoped by `user_id`. Scoped to self-host (single user / household), per the topology above. See [ADR-0003](docs/adr/0003-multi-user-auth.md). |

## Functional requirements

### F1. Statement import

Three families of import, each with its own parser registered in a
`StatementParser` strategy table:

**1. Spending statements (credit-card + bank)**
- Formats: CSV and password-protected PDF.
- Issuers (shipped): Axis (CC), ICICI (CC). HDFC was dropped from v1 — the card isn't
  held, so no redacted fixture can exist for it, and the parser-discipline rule requires
  one. Bank-statement parsers are unbuilt for every issuer; see §Verification step 6 for
  how F4a is exercised without one.
- Output: spending `transactions` rows in INR.
- Parser contract ([ADR-0010](docs/adr/0010-parsed-statement-return.md)): `StatementParser`
  yields the parsed rows **plus** statement-level metadata — opening/closing balance and
  billing period — read off the same file. The metadata is optional per layout: a statement
  whose printed layout carries no balance block yields it all-`None`, which is a legitimate
  result, never a parse failure. See §F4a case 5 for what it's used for.

**2. Investment transactions (canonical CSV)**
- Format: a transaction-level CSV exported from a broker / AMC — seeded against the
  Zerodha Console *Tradebook*; other brokers usually import unmodified because a
  repo-tracked `HEADER_ALIASES` map resolves their column spellings (no renaming).
- One canonical field set (`date`, `type`, `symbol`, `units`, `price`; optional
  `amount` / `fees` / `name` / `asset_class` / `exchange`); unknown columns ignored.
- INR-only in v1 — a single non-INR row rejects the whole file (USD + FX land in v0.5).
- Output: `instruments` rows (keyed by normalised `symbol`) + `investment_transactions`
  rows. Importable types: buy / sell / sip / dividend / bonus. `split` / `switch_in` /
  `switch_out` stay in the schema vocabulary but have no v1 import path (rejected here
  and on manual entry).

**3. INDmoney US transactions**
- Format: INDmoney's transaction export — typically CSV from their app's "Export
  Data" feature (PDF as fallback).
- Output: `instruments` rows (US tickers like `AAPL`, `VOO`) marked
  `currency = USD`, `exchange = NASDAQ` / `NYSE`; `investment_transactions` in
  USD with the FX rate stamped at transaction date.

**Common flow** (all three):
1. User picks an account (or creates one), uploads file, supplies PDF password if
   needed.
2. Backend routes to the right parser → normalised rows.
3. Dedup step (F4).
4. Auto-tag step (F3) — only applies to spending transactions.
5. Review screen → user confirms.

**Upload limits (boundary validation).** Uploads are size-capped (configurable; default in
the single-digit-MB range — well above any real statement) and refused with a **413** *before
the body is buffered* — at the reverse proxy plus a streaming guard in the handler (don't gate
on `Content-Length` alone: absent on chunked encoding, spoofable). Applies to every import
path; a general boundary check that matters most on any internet-exposed instance (e.g. a
self-host on a public IP), where an unbounded upload is a trivial memory / disk DoS.

### F2. Manual transaction entry

- Fields: `date`, `account`, `amount` (signed), `merchant` (free text), `category`, `notes`,
  `transaction_type` (spend / income / transfer).
- Sign rule ([ADR-0009](docs/adr/0009-refund-as-signed-spend.md)): `spend` accepts either sign
  — negative is an outflow, **positive is a refund** — `income` requires a positive amount,
  `transfer` accepts either sign, and zero is rejected for all three. A refund is not its own
  type; it is a `spend` row.
- Transfers between user's own accounts use `transaction_type = transfer` and are
  excluded from spend reports.

### F3. Auto-tagging (exact-match v1)

- Maintain a `merchant_tag_map` table: `(normalized_merchant, category, hit_count, last_used)`.
- Normalisation (shipped): **lowercase**, strip leading/trailing whitespace, collapse
  multiple spaces. That's all of it — `services/merchant.py` is the single source of truth.
- Future: stripping trailing transaction IDs / dates / reference numbers via regex
  (e.g. `swiggy*blr*12345 2026-04-12` → `swiggy`). This is **not** a code edit: the
  normalised string is the stored key for `transactions.fingerprint` and both merchant
  memory maps, so changing it requires the recompute migration in
  [ADR-0006](docs/adr/0006-f4-dedup-key.md) §Recompute procedure. See the CHANGE HAZARD
  block in `services/merchant.py`.
  **Shipped, without touching this function** ([ADR-0011](docs/adr/0011-merchant-alias-layer.md)):
  a `merchant_alias` table resolves a canonical merchant *downstream* of normalisation, and the
  two memory maps re-key on it at read time, leaving the fingerprint on `merchant_normalized`.
  Matching is **token-boundary contains** — both strings are split on runs of non-alphanumerics,
  and an alias fires when its token sequence is a contiguous subsequence of the merchant's tokens
  (so `upi/swiggy/9876@ybl` resolves to `swiggy`, but `ola` does not false-merge into
  `chocolate hut`). Conflicts resolve by longest-pattern-wins, a total order derived from the
  data — not a user-editable priority field. Authored on `/settings/rules`; every new user is
  seeded at registration with ~96 Indian-merchant dictionary entries (`is_seeded=True`,
  `hit_count=0`), fixing the cold start this measurement below describes. That route is what makes
  the miss rate worth quoting in the first place: normalisation is lowercase + whitespace-collapse
  only, so before aliasing, auto-tagging **could not fire at all** on a merchant whose descriptor
  carries an order id, RRN or VPA — most Indian CC and UPI rows.
- On import: for each new row, look up `normalized_merchant`; if found, prefill category
  with highest-hit-count entry.
- "User decision" fires `merchant_tag_map` upsert (insert or `hit_count++`) at these sites.
  A decision is taught **once, at one authoritative site per lifecycle** (see ADR-0004):
  - F2 manual POST with non-null category (immediate user-yes).
  - PATCH `category_id` on a **board** row (`confirmed_at IS NOT NULL`) when the value
    actually changed (post-board correction). PATCH on a **pending** review-queue row
    does *not* teach — pending edits learn only at commit (below), so discarding or
    cancelling a row leaves no orphaned map entry.
  - Bulk commit of an import batch (each row in the committed list = a user-yes,
    even if the category was prefilled by import auto-tag — passive accept counts
    as a positive signal), **except** rows defaulted to *Other* because their
    category was null or archived at commit (F4a reconciliation): those don't teach,
    so an archived bucket is never re-learned.
- Acceptance metric (`GET /dashboards/tagging-stats`) uses **current-health**
  semantics: rows whose frozen suggestion points at a since-archived category are
  excluded from both numerator and denominator (see ADR-0004).
- **Manual pin (rule authoring — `/settings/rules`)**: the user can *pin* a
  merchant→category (and merchant→label, F3a) association so it always wins the
  prefill regardless of `hit_count`. Implemented as a `pinned` flag on the map row
  that the reducer orders ahead of the learned rows (`pinned DESC` before
  `hit_count DESC`). This is **authored** memory (no originating transaction
  decision), as opposed to the **learned** upserts above; the learning path never
  sets the flag, and pin/un-pin toggle *only* the flag (never `hit_count` /
  `last_used`), so un-pinning reverts to the untouched learned ranking. Still
  merchant-**exact** — not the regex rules below.
- Out of scope v1: fuzzy/edit-distance match, **regex** rules, wildcards, ML classifier, and any
  condition on amount, account or date (research §7's killed rules-engine expansion; re-checked
  against the alias layer at research §13.7 without collision). **Token-boundary contains
  matching, scoped to the merchant string alone, is in scope** — the alias layer above and the
  merchant-exact manual pinning above both are (research §13.10).

### F3a. Transaction labels (user tags)

Freeform, user-applied labels on spending transactions — cross-cutting tags
orthogonal to the single `category` (a *Food* txn can also be `#online`,
`#restaurant`, `#travel`). **Distinct from F3 auto-tagging** (which learns
merchant→*category*); labels are **manually** applied in v1. User-facing name is
"Tags" (rendered with a leading `#`); the code/data domain is named `label` to
avoid colliding with the F3 `merchant_tag_map` / `tag_service` "tag" namespace.

- **Model**: `labels (id, user_id, name)` + a many-to-many join
  `transaction_labels (transaction_id, label_id, user_id)`. Names are normalised
  (lowercase, leading `#` stripped, whitespace collapsed, `;` removed, ≤64 chars)
  and unique per user. **Hard-delete**: removing a label removes it from every
  transaction (join rows cascade).
- **Apply**: labels are set on a transaction at F2 manual create, on PATCH
  (replace-set semantics — the submitted list becomes the exact set), and inline
  in the F1 import review queue. Typing suggests existing labels; an unrecognised
  name is **auto-created on save** (get-or-create), never per keystroke.
- **Filter**: the expenses board (F8 view) filters by a single label, alongside
  account / category / month.
- **Manage**: Settings → Tags lists all labels with rename + delete.
- **Isolation**: `transaction_labels` carries `user_id` with composite same-user
  FKs to both `transactions` and `labels` (the ADR-0002 `transfer_pair_id`
  pattern), so a link can never cross tenants.
- **Out of scope v1**: label colours, labels on investment transactions, bulk
  apply, multi-label (AND/OR) filtering.
- **Phase 2 (shipped)**: F3 auto-tagging also learns merchant→label (a
  `merchant_label_map` sibling to `merchant_tag_map`) and pre-fills labels whose
  `hit_count` clears the confidence bar in the review queue — so "auto-tagging"
  now covers categories *and* labels. Exact-match only, spend-gated (a refund
  included — [ADR-0009](docs/adr/0009-refund-as-signed-spend.md)), no decay
  (mirrors merchant→category learning).

### F4. Duplicate detection

- Per-row fingerprint: `sha256("\x1f".join(date_iso, amount_paise, normalized_merchant, account_id))`
  — the four fields joined by `\x1f` (ASCII Unit Separator), per [ADR-0006](docs/adr/0006-f4-dedup-key.md).
  The separator is load-bearing: plain concatenation left two ambiguous boundaries between
  variable-length values, so merchant `AMAZON1` + account `2` hashed identically to `AMAZON` + account `12`.
- Stored on the transaction row alongside an `occurrence` ordinal, unique-indexed per
  `(user_id, account_id, fingerprint, occurrence)`. The hash carries *identity*; `occurrence`
  carries *multiplicity*, so two genuinely-distinct transactions agreeing on all four fields —
  two auto rides at the same fare on one day — can both be stored. Nothing positional ever
  enters the hash.
- Import dedup is a per-fingerprint **multiset difference**, not a set-membership test: if the
  account already holds `n_db` rows for a fingerprint and the file yields `n_file`, then
  `max(0, n_file - n_db)` rows are staged with ascending `occurrence` and the rest are counted
  as skipped; the count is returned to the UI. Manual entry (F2) does *not* assign `occurrence`
  and so still 409s on an identical row — the importer can prove distinctness by counting the
  file, a lone POST cannot.
- A second stored hash, `origin_fingerprint`, records **which statement line produced a row**
  ([ADR-0007](docs/adr/0007-transaction-field-editability.md)). The four identity columns are
  user-editable, so `fingerprint` is recomputed on an edit — and without a frozen copy of the
  original the importer would read that edit as a deletion and re-stage the pre-edit row,
  giving two rows for one transaction whose fingerprints differ by construction. The file-dedup
  prefetches therefore key on `COALESCE(origin_fingerprint, fingerprint)`. It is stamped once,
  at stage time, by the statement importer only: manual entry, transfer legs, F4a and backup
  restore all leave it `NULL`, because a row with no external artifact is honestly keyed by its
  own current values. Never unique, never indexed, never recomputed.

### F4a. Reconciliation rules

Four cases where the import pipeline needs explicit rules — silent defaults here
would produce wrong totals.

**1. Credit-card bill payment showing up twice** (CC statement payment + bank debit).
- Detection: bank-account transaction and CC-account transaction with (a) amounts
  equal in magnitude and opposite in sign, (b) dates within ±2 days, (c) the bank
  side matches a CC-payment merchant regex (`CC PAYMENT|CREDIT CARD PMT|HDFC CC|
  ICICI CC|AXIS CC`) OR the CC side matches `PAYMENT RECEIVED|THANK YOU FOR PAYMENT`.
- Action on detection: both rows auto-classified as `transaction_type = transfer`,
  linked via a generated `transfer_pair_id` on both rows. Excluded from spend totals.
- User sees a "Linked CC bill payment" banner on each row **on the board, once the
  batch is committed** — not in the review screen. The link is created *by* the
  commit: `auto_link_cc_bill` runs after the pass that stamps `confirmed_at`, and
  the review screen serves only unconfirmed rows, so a pending candidate provably
  never carries a `transfer_pair_id` yet. Breaking the link, if the auto-detection
  got it wrong, is likewise a board action on the committed row (§Verification 6).
- This rule activates only when a CC account has been associated with a parent bank
  account (F6's optional relationship). No association → no auto-link, user resolves
  manually.

**2. CSV import references an instrument already entered manually.**
- Identity is the normalised `symbol` (broker ticker, upper-cased) — the model has a
  single `symbol` field, no ISIN column. A CSV row whose symbol matches an existing
  active instrument **reuses** it; no duplicate, no merge prompt.
- Re-import is idempotent: an identical file short-circuits on its content hash, and an
  overlapping different file dedups per row by a fingerprint keyed on the resolved
  `instrument_id` (so ticker case / rename can't double-count).
- No fuzzy-name matching in v1 — a genuinely new symbol creates a new instrument; the
  user can rename or archive via Settings → Instruments.

**3. Refunds.**
- A refund is not its own `transaction_type` — it is a `spend` row carrying a
  *positive* `amount_paise`, derived at read time rather than stored
  ([ADR-0009](docs/adr/0009-refund-as-signed-spend.md)). **The sign is load-bearing,
  not the type.** Every F8 aggregate discriminates `spend` rows by sign — negative
  into gross spend, positive into refunds — and there is no separate `refund` value
  left to route incorrectly.
- Refunds preserve the original merchant and category. A Swiggy refund auto-tags to
  Food. This is correct for personal-finance accounting: category totals are
  *signed sums*, so the refund naturally reduces the month's Food spend — and
  because a refund is a `spend` row by construction, it cannot drift into income's
  bucket the way a mistyped row once could.
- A CC statement credit is still ambiguous on arrival: a merchant refund and a
  cashback credit are indistinguishable from the statement text alone, and cashback
  is legitimately *income* (§F5). The parser's classification is therefore a best
  effort (positive amount → tentatively `spend`, i.e. a refund, unless the merchant
  text names cashback, in which case `income`), and **the user corrects it** by
  picking a category — a spend category keeps it a refund netting against that
  category, an income category retypes it to `income`. This is now a *category-kind*
  choice, not a type choice: every user-visible column, `transaction_type` included,
  is editable on `PATCH /transactions`
  ([ADR-0007](docs/adr/0007-transaction-field-editability.md)), but the review queue's
  merged spend/income category picker is what most users act through.

**4. Statement re-import after the issuer corrects a row.**
- Fingerprint includes amount, so a corrected amount produces a *new* fingerprint
  and the new row imports alongside the old (now-wrong) one.
- v1 resolution: **manual** — user edits the wrong row via the transaction UI, or
  deletes it. No auto-supersede detection in v1; the case is rare and the auto-
  detection logic (amount-close-but-not-equal + same date + same merchant) has
  too many false positives to be safe.
- v1.5 idea (not committed): a "Reconcile" tool that flags suspected supersedes
  side-by-side for user confirmation.

**5. Statement closing balance vs our record.**
- Detection: a **window-delta** comparison, not the running-balance-vs-history check the
  §Roadmap entry this supersedes once proposed. `expected = closing_balance − opening_balance`,
  both read straight off the statement; `actual = Σ(amount_paise)` over the account's confirmed
  transactions **plus this batch's still-pending rows**, restricted to the statement's own
  `[period_start, period_end]` window. `delta = actual − expected` (signed, exact integer paise —
  no tolerance). Window-scoping removes the dependence on the account's whole prior history, so a
  first-ever import reconciles without it.
- Credit-card sign convention (stated, not derived): **negative = owed.** A printed "Total Amount
  Due" of ₹X stores as **−X paise** — a card's `opening_balance_paise`/`closing_balance_paise`
  use the same convention as the account's own balance. A `CR` (overpaid) closing balance stores
  positive.
- Action on mismatch: **warn, never block** — the delta persists on the `ImportBatch` row and
  surfaces as a review-screen banner plus a pending-batches feed badge; the commit still succeeds
  regardless. No acknowledge flag, no blocked commit.
- A statement whose layout prints no balance block (the Axis Flipkart co-branded layout) imports
  with the check simply not run — `NULL` delta, never a parse error (ADR-0010).
- **Known false-positive classes, accepted, not corrected:** (a) a manually-entered (F2) row for
  a transaction the statement also lists produces a mismatch — the statement lists everything the
  issuer recorded, and F4's per-account dedup can't catch a manual row with different merchant
  text; (b) a row **discarded** at review (most commonly an investment-transfer debit that
  belongs to F7, not this board) permanently removes its amount from `actual` with no trace — a
  hard delete, not a soft one, so the delta cannot self-correct. Both are informative, not noise;
  warn-never-block makes them cheap. Case (b) is qualified, not silently unexplained: the review
  screen also reports *how many* of the batch's originally-staged rows were later discarded (a
  live count against the batch's frozen `imported_count`,
  `reconciliation_service.rows_removed_since_import`), so a routine monthly SIP discard reads as
  "N rows removed since import," not an unexplained mismatch.

### F5. Categories

- Categories are **typed** by a first-class `kind` column (`spend` | `income`), set at
  create and immutable thereafter (there is no PATCH for `kind`). Spend categories serve
  `spend` transactions of either sign — a refund included
  ([ADR-0009](docs/adr/0009-refund-as-signed-spend.md)); income categories serve
  `income`. Each row also carries a user-pickable `color` (`#rrggbb`, nullable — see the
  inheritance rule below; `categoryColorVar` falls back to `--muted-foreground`, it does
  **not** derive a hue from the id).
- **Two levels, and no more** ([ADR-0012](docs/adr/0012-category-hierarchy.md)). A
  category carries a nullable self-FK `parent_id`. `parent_id IS NULL` is a *parent*; a
  row pointing at one is a *subcategory*. A subcategory can never itself be a parent —
  a create or PATCH that would nest three deep is a 422 — and its `kind` must equal its
  parent's. Depth is enforced in `api/v1/categories.py`, not in the schema layer.
- **Both levels are taggable.** A transaction may point at a parent or at a subcategory;
  there is no leaf-only rule, so the F3/F4a "Other" default and every pre-hierarchy row
  stay valid untouched. Aggregates roll a parent up **exactly one hop** at the query
  boundary: `GET /transactions?category_id=<parent>` matches the parent *and* its
  children, and the F8 by-category surface groups on the parent with a subcategory
  drilldown. There is no recursive walk anywhere.
- Registration provisions a 2-level default taxonomy. `services/provisioning.py`
  `_DEFAULT_SPEND_TAXONOMY` / `_DEFAULT_INCOME_TAXONOMY` are the live path **and the only
  place the subcategory names are written down** — read them rather than copying the list
  into prose here or into a test.
  - **spend — 9 parents**: Food & Dining, Household & Living, Bills & Utilities,
    Commute & Transportation, Shopping & Lifestyle, Family & Social,
    Savings & Investments, Loans & Settlements, Other.
  - **income — 1 parent**: Income, holding Salary / Freelancing / Cashback /
    Investment Returns / Rental Income / Other.
  - An existing user is brought to the same shape by migration 0034, which **reparents**
    the old flat defaults under the new parents. It renames nothing and deletes nothing,
    so a transaction tagged "Groceries" keeps its category and merely gains an ancestor.
- **`color` inherits one hop.** A subcategory with `color IS NULL` renders in its
  parent's hue, so a family reads as one colour; siblings are separated by a derived
  shade, never by an unrelated hue. Every seeded subcategory is `NULL` — a seeded
  subcategory carrying its own hex is drift, and `tests/test_migration_parity.py` is what
  pins that.
- Note "Transfer" is **not** a category name — that moved to the `transaction_type`
  dimension. **"Income" now is one**: it is the single income parent. And "Other" still
  exists **twice**, once per `kind` — as a spend *parent* and as a subcategory under
  Income. `kind` participates in the active-name unique index, so a name-only lookup for
  the F3/F4a Other-default is ambiguous and must also filter `kind`.
- User can add / rename / **reparent** / soft-delete. **Archiving cascades one level** —
  archiving a parent archives its active subcategories in the same request. There is
  still no hard delete: `transactions.category_id` is a plain FK, and the `archived_at`
  contract is the only thing keeping it from breaking.

### F6. Accounts

- Account types: `credit_card`, `bank`, `cash`, `investment`.
- Per-account fields: `name`, `type`, `issuer` (nullable), `last4` (nullable for cash),
  `opening_balance`, `currency` ("INR" hardcoded).
- **An `investment` account is a placeholder, and its `opening_balance` must be zero.**
  It exists to group and label a broker relationship; it holds no money. Holdings live
  in `instruments` / `investment_transactions` (§F7), transactions and transfers are
  refused on the type, and its balance is therefore excluded from net worth (§F8 view 4)
  — otherwise recording "Zerodha, ₹50,00,000" and then importing the CSV for those same
  holdings reads as ₹1 crore. A non-zero opening balance is a 422 at create.
  **Un-itemised balances (PPF/EPF, and anything else not yet broken into holdings) are
  recorded as an *instrument*, not here** — pick a manual-NAV `asset_class` (`nps`,
  `fd`, `bond`, `gold`, `other`; §F7 lists which classes auto-price and which don't) and
  enter the value as its NAV.
- A `credit_card` account belongs to a parent `bank` account only if the user wants to
  model auto-pay relationships — optional, not required.

### F7. Investment tracking

- **`instruments`** — one row per scheme/ticker. Fields:
  - `symbol` — the user-facing handle and the dedupe key (e.g. an AMC scheme symbol,
    `AAPL` for a US stock). Identity remains symbol-keyed (see F4a).
  - `isin` (nullable, e.g. `INF209K01YV4`) + `amfi_code` (nullable) — identity keys that
    match an Indian MF to its AMFI NAVAll row. `isin` comes from the import CSV *or* the
    Add-instrument form — write-once either way (fill-if-null on create, 422 on a
    conflicting PATCH; delete and re-create to change it), because without it a
    hand-registered fund can never be auto-priced. `amfi_code` is back-filled from NAVAll
    on first NAV match, and that first match reports the matched scheme name so a
    mistyped-but-valid ISIN shows up instead of silently pricing the holding off another
    fund.
  - `name`, `asset_class` (`indian_equity` / `indian_mf` / `us_equity` / `us_etf` /
    `fd` / `bond` / `nps` / `gold` / `other`).
  - `currency` (`INR` or `USD` in v1).
  - `exchange` (`NSE` / `BSE` / `MFCentral` / `NASDAQ` / `NYSE` / `OTHER`).
  - `current_nav` (in native currency) + `nav_updated_at`. v1 auto-prices Indian MFs,
    Indian equities, US equities and US ETFs: Indian-MF NAVs from AMFI NAVAll (matched
    by `isin`), the rest from a public quote source (Yahoo, by `symbol` / `isin`) —
    `nav_snapshot_service._QUOTE_CLASSES` is the authoritative list. Only the classes
    with no auto source — fd / bond / nps / gold / other — stay user-entered. USD
    holdings roll up through the FX layer below (not deferred).
- **`investment_transactions`** — `date`, `instrument_id`, `transaction_type`
  (buy / sell / sip / dividend / bonus / split / switch_in / switch_out), `units`,
  `price_per_unit_native`, `amount_native`, `fees_native`, `fx_rate_to_inr`
  (snapshot at transaction date — `1.0` for INR rows).
- **`fx_rates`** — daily INR↔USD rate snapshots, keyed by date. Used for:
  (a) stamping `fx_rate_to_inr` on new investment transactions, (b) converting
  `current_nav` to INR for portfolio rollups. Populated from `frankfurter.app` at
  seed time or by manual trigger — **synchronous, no scheduler in v1** (`api/v1/fx.py`),
  and the endpoint currently has no in-app caller, so nothing advances the cache while
  the app runs. Reads carry the last-known rate forward with no age cap, which is why
  `fx_staleness_days` / `fx_unavailable_count` are surfaced as honesty flags.
- **XIRR**: computed via `pyxirr` per holding in *native* currency, and at portfolio
  level in *INR* (after FX-adjusting every cashflow). Recomputed on demand —
  `pyxirr` is fast enough at personal scale that caching isn't needed.
  **Scope (current-holdings):** the portfolio number is over *current* (still-held,
  NAV-bearing) holdings — "the return on what I still hold and can price" — not a
  lifetime realized return. A fully-exited instrument (`net_units == 0`) is absent
  from the sourced set, so its buy/sell cashflows can't leak in and unbalance the
  portfolio-wide figure. Lifetime realized return is F11's job (it reads
  `realised_lots`), not this number.
- **Allocation %**: `current_value_inr / total_portfolio_value_inr`. Asset-class
  donut splits into `indian_equity`, `indian_mf`, `us_equity`, `us_etf`, etc.
- **Benchmark / performance** (v1, scalar): a `benchmarks` reference table — curated index
  *funds* (INR, TRI growth NAV, post-expense) keyed by AMFI scheme code — plus a
  `benchmark_nav` price cache. The "am I beating the market" answer replays the user's
  exact signed cashflows into a chosen benchmark fund and reports
  `alpha = portfolio_xirr − benchmark_xirr` and the rupee gap. Benchmark NAV history is
  backfilled at seed time, **never fetched on the request hot path**. Surfaced by F8 view 5.

**Transaction-type rules** (these need to be explicit or the math breaks):

- **`buy`** — adds units, decreases cash (or just records cost). `price_per_unit_native`
  and `amount_native` both required.
- **`sell`** — removes units. Cost basis released using **FIFO** (first lot bought
  is the first sold). Realised gain/loss = `sale_proceeds_native − cost_basis_native`.
  FIFO chosen because: (a) it's what Indian capital-gains rules use for equity / MF
  taxation anyway, (b) it's the simplest to reason about without per-lot UI.
  The FIFO match must be **persisted**, not just computed in passing: each sell (and
  each `switch_out`) records which buy lot(s) it consumed — `(matched_buy_id,
  matched_units, matched_cost_paise, buy_date)` per lot — so realised gain *and holding
  period* are recoverable per sold lot (the input to the F11 capital-gains statement).
  Today's in-memory FIFO (`holdings_service._consume_fifo`) pops lots only to compute the
  *remaining* cost basis and discards the match, so this is net-new persistence, not a
  re-use of existing output.
- **`sip`** — alias of `buy`, kept as a separate type only so reports can show
  "regular SIPs" distinct from one-off buys.
- **`dividend`** — **cash payout, not a buy**. Records the dividend amount per
  instrument; does *not* create new units. Dividends *do* count in XIRR cashflows
  (they are positive cashflows out of the holding back to you). Stored as a row with
  `units=0`, `amount_native > 0`, `transaction_type='dividend'`.
- **IDCW reinvestment** — an Indian MF dividend-**reinvestment** plan is recorded as a
  `dividend` row (the income) **plus a linked `buy` row** (the acquisition at that
  date's NAV), sharing a `pair_id` in both directions, via
  `POST /investment-transactions/reinvestment`. One row cannot carry both: conflating
  income with acquisition is precisely what breaks FIFO holding periods, and the `buy`
  leg must open its own lot with its own cost basis and acquisition date. The pair nets
  to **zero** in XIRR (`+amount` then `−amount` on one date), which is correct — no
  money entered or left the portfolio — while units and invested both rise. Out of scope
  for CSV import (the format cannot express the link, so a partial failure could persist
  one leg alone); a CSV `dividend` carrying units is rejected with a reason naming the
  endpoint. TDS-split reinvestment (gross ≠ reinvested) is a separate future feature.
- **`bonus`** — issuer gives free units (no cashflow). Adds units at zero cost; the
  average cost of the holding therefore goes down. XIRR unaffected (no cashflow).
- **`split`** — units multiply by the split ratio; cost basis per unit divides by
  the same ratio. Implemented as an in-place adjustment of prior `buy`/`sip` rows
  belonging to that instrument before the split date (so FIFO lot accounting stays
  intact). XIRR unaffected.
- **`switch_in` / `switch_out`** — a switch between MF schemes is recorded
  as `switch_out` from the source (acts like a `sell` for FIFO accounting) and
  `switch_in` to the destination (acts like a `buy`). Both stamped with the same
  `pair_id` so reports can render them as one event if desired. Reserved in the
  schema but with no v1 import path — rejected by both CSV import and manual entry (the
  CAS importer that once produced them was dropped in favour of the canonical CSV).
  `pair_id` is the generic two-leg link (renamed from `switch_pair_id` in migration
  0026); the pair's *kind* is read off its member types — `(dividend, buy)` for an IDCW
  reinvestment, `(switch_out, switch_in)` for a switch — with no discriminator column.

**Realised-gain classification** (feeds F11, computed from the persisted lot-match): each
sold lot is tagged from `sell_date − buy_date` and the instrument's `asset_class` — **STCG**
if a listed-equity / equity-MF lot is held ≤12 months, **LTCG** if held longer; debt lots
(`asset_class ∈ {bond, fd}`) bought on/after **2023-04-01** are flagged slab-taxed regardless
of holding period (§50AA). Equity lots bought before **2018-02-01** carry a grandfathered cost
(§112A deemed-cost rule). Rates and thresholds (the 12-month line, the ₹1.25 L LTCG exemption,
the slab cut-off date) shift with each Union Budget → they live as **named constants in a
config module**, never inline literals. Three accounting subtleties must flow through the
lot-match or the statement diverges from the AMC's own CG report: `bonus` units carry **zero
cost basis** and start their *own* holding period at the bonus date; `split` preserves the
original lot's holding period (it adjusts prior buys in place, above); `switch_out` is itself
a realised-gain event.

**Out of scope v1**: per-lot manual override of FIFO selection (i.e. "I want to sell this
specific lot for tax-loss harvesting" — the realised-gain statement is reporting-only, see F11;
loss set-off / carry-forward and a tax-loss-harvesting view are v2, see §Roadmap). Add when
actually needed.

### F8. Dashboards

**v1 (5 views):**

1. **Live portfolio summary tiles** (top of every page) — current portfolio value in
   INR, today's change (₹ and %), this-month change, this-year change. Auto-refreshes
   per F9. The spend-side summary strip alongside these shows the current month's
   spend total, a month-over-month delta against the same day-span last month, a
   weekly spend sparkline, and the month's income. **No savings-rate tile ships**
   — `net / income` would be a ratio over the existing `GET
   /dashboards/period-totals`, but it is not built; earlier drafts of this
   document asserted it in three places.
2. **Monthly spend by category** — stacked bar or pie, current month default with
   month-picker. **Grouped by parent category** (§F5), with two drilldowns: click a
   parent to see its subcategory split, click either level to reach the transaction list
   filtered to that category. A parent's filter includes its children, so the drilldown
   total and the resulting transaction list agree.
3. **Weekly / monthly spend bar** — toggle between weekly and monthly aggregation for
   the last N periods.
4. **Net worth** — cash / bank balances + investment current values. **Two account
   types are excluded outright**, for opposite reasons. **Credit cards** are not
   contributed as a clamped liability: no bill payment is ever recorded against a
   card, so its balance is accumulated *spend*, not debt, and folding it in would
   subtract a full month's card spend from net worth. A card's calendar-YTD spend
   rides along separately on `spend_ytd_paise`. **Investment accounts** are
   placeholders holding no money of their own (§F6 pins their opening balance to
   zero), and the value they would represent is already counted as the portfolio's
   current value — adding both double-counts it. The accounts list still reports
   each raw signed balance, excluded types included. Only `bank` and `cash`
   contribute, and they contribute their signed balance, so a bank overdraft
   legitimately reduces net worth. Computed query-time from `transactions` +
   `investment_transactions` + latest `current_nav` (no separate snapshot table —
   see Data model rationale). **Scalar only** — the "over time" trend is not built;
   only the headline figure ships.
5. **Portfolio vs benchmark (performance)** — the "am I beating the market" view, and the
   headline of the investment side. v1 ships the **scalar** answer: whole-portfolio XIRR
   (over *current* holdings — fully-exited positions excluded; see §Data model XIRR scope),
   the benchmark XIRR (your exact cashflows replayed into a chosen INR index *fund* — TRI
   growth NAV, post-expense, labelled as a fund, not the raw index), the **alpha**
   (difference in % points), and the **rupee gap** ("you have ₹Y; the index fund would be
   ₹X"). Computed query-time from the F7 cashflows + the `benchmark_nav` cache. A moving
   portfolio-vs-benchmark *line chart* and the up-to-3-benchmark overlay are **deferred to
   a later milestone**: a time-series portfolio curve needs dense per-holding historical
   NAV (see §Roadmap), and the cheap money-weighted shortcut degenerates to a misleading
   flat line — so v1 delivers the one trustworthy number instead of a wrong picture.

**v1.1 (deferred, single weekend after v1 ships):**

6. **Spend trend over time** — line chart, last 12 months, optional category filter.
7. **Investment portfolio breakdown** — table of holdings (units, avg cost, current
   value, XIRR, % allocation) + donut by asset class.

v1.1 features are split out because v1 delivers the core "what did I spend, what am I
worth" loop; v1.1 adds drilldown and analytics. Both v1.1 dashboards depend on the
same underlying APIs — splitting is purely scope discipline, not extra work.

### F9. Live / reactive updates

**What "live" actually means in v1** — and what it doesn't:

- "Live" = **the dashboard reflects the latest saved data without me hitting reload**.
  It does *not* mean ticking market prices. v1 NAVs are refreshed on demand, not streamed:
  a manual-trigger snapshot — the sync icon on **/portfolio** (NAVs + benchmarks) or
  **/holdings** (NAVs only), hitting `POST /instruments/refresh-navs` — fetches
  Indian-MF NAVs from AMFI NAVAll and Indian-equity / US-equity / US-ETF prices from a
  public quote source, while the classes with no auto source (fd / bond / nps / gold /
  other) stay user-entered. "Today's change" means
  *today's NAV* minus *the most recent NAV before today*. If there's no NAV for today,
  today's change is "no data" — not a stale number. (`nav_updated_at` records the NAV's
  *valuation date* on every write path — the source's NAV date for auto-snapshot
  holdings, the client's `nav_as_of` for manually-entered ones — so the change math and
  the staleness flags read one meaning, not two.)
- **Propagation mechanism**: TanStack Query holds the cache.
  - On mutation (save transaction, update NAV): the client calls
    `queryClient.invalidateQueries(["portfolio", "summary"])` → tiles refetch
    immediately → UI reflects the new state within ~one round-trip (target ~150ms
    on localhost, ~400ms when hosted).
  - On idle: `refetchInterval: 30_000` + `refetchOnWindowFocus: true` keeps the tiles
    fresh if other tabs / processes have written.
- **Performance targets** (honest, not aspirational):
  - **Summary tiles endpoint** `/api/portfolio/summary` — <200ms cold, <50ms warm.
    Returns scalars (current_value_inr, today_change_inr, month_change_inr, year_change_inr).
    Computed via SQL aggregates + a single `pyxirr` call on the current holdings.
  - **Heavy endpoints** like `/api/portfolio/holdings` (drilldown) or
    `/api/imports/<batch_id>` (returning hundreds of rows) — <1s. These aren't
    polled; they're loaded once when their page mounts.
- **v2 push channel**: a FastAPI **Server-Sent Events** endpoint (`/sse/portfolio`)
  pushes change events whenever the backend writes to `investment_transactions` or
  `instruments`. React subscribes and invalidates queries on receipt. Lower latency,
  no polling waste. Deferred because polling at 30s intervals is sufficient for
  personal-scale single-user use.

### F10. Export & Google Drive sync

- **Export format (shipped)**: zipped CSV — `metadata.json` plus exactly three members,
  `accounts.csv`, `categories.csv`, `transactions.csv`. This is deliberately the
  **restore-faithful spend subset**, not a whole-DB dump: investments round-trip
  separately via `/imports/investments`, and merchant memory is relearned rather than
  exported. `parsers/backup_csv.py` owns the column tuples as the single source of truth
  and the exporter imports them, so the two halves cannot drift.
  Foreign keys are exported as both the internal id and a human-readable label (e.g.
  `account_id` + `account_name`) so the CSV is usable in Excel without lookups.
  `transactions.csv` carries a `labels` column (the row's F3a tags as `;`-joined names)
  in place of the former free-text note. `categories.csv` carries `parent_name` — the
  §F5 hierarchy travels as a **label, not an id**, so the link survives a restore into a
  database where ids differ. A parent whose child has transactions is pulled into the
  export even if the parent itself has none, otherwise the child would restore orphaned.
- Out of scope v1: `merchant_tag_map.csv`, `instruments.csv`,
  `investment_transactions.csv`, `fx_rates.csv`. A full-table backup is a scoped feature
  with its own verification step, not a widening of this one.
- **Local download**: a "Download backup" button on Settings produces
  `fin-tracker-backup-YYYYMMDD-HHMMSS.zip` and streams it to the browser.
- **Drive sync**: a "Sync to Drive now" button on the same screen uploads the same
  zip to a `FinTracker/` folder in the user's Drive.
  - **Auth**: Google OAuth via `google-auth-oauthlib` with scope
    `https://www.googleapis.com/auth/drive.file` (app-created-files only — the app
    cannot read the user's other Drive content). Token (with refresh token) stored
    encrypted on disk under the app data dir.
  - **One-time setup**: user creates a Google Cloud project, enables Drive API, and
    pastes `client_id` + `client_secret` into Settings. Documented in README — this
    is necessary because we don't ship the app's own OAuth client (which would mean
    publishing it).
  - **Retention**: keep the last N (default 30) backups in Drive; older ones pruned
    after a successful new upload.
- **Restore (shipped)**: `POST /backup/import` plus a "Load backup" control on
  Settings → Backup. **Additive and non-destructive** — rows already present are skipped
  by recomputed fingerprint, so re-importing after adding transactions tops up rather
  than wiping. Accounts and categories are matched-or-created; transfers are relinked.
  There is no CLI script (`scripts/` holds only `package_source.py`,
  `redact_fixture.py`, `refresh_skills.py`), and none is wanted — a second CSV-load
  implementation would drift from `backup_import_service`'s fingerprint / occurrence
  handling. Restore *from Drive* remains v2.
- **Caveat called out for the user**: CSVs cover portability (open in Excel) and
  *most* of backup — but losing the live SQLite file mid-session between syncs is
  still a data-loss window. v2 will add a full SQLite-file dump path for true
  byte-for-byte backups; v1 trades that for simplicity.

### F11. Tax statements & reporting

Year-end reporting built on the F7 realised-gain lot-match + the dividend rows. Framing
throughout: **a statement to reconcile against your AMC / broker CG report and your AIS /
Form 26AS before filing — not tax advice, and not a return.** Two FY-scoped reports
(financial year = 1 Apr – 31 Mar):

- **Capital-gains statement** — one row per sold lot: instrument, buy date, sell date, units,
  cost basis, proceeds, realised gain/loss, and a bucket tag (**STCG** / **LTCG** /
  **debt-slab**) from the F7 classification. Per-bucket subtotals, plus the equity-LTCG
  **₹1.25 L exemption** surfaced as an explicit line (gross LTCG → exemption applied → net
  shown for *reconciliation*, not as a filing figure). Computed query-time from the persisted
  lot-match; no cache table.
- **Dividend income summary** — rollup of `transaction_type='dividend'` rows per FY as *Income
  from Other Sources* (slab-taxed since FY 2020-21), for AIS / Form 26AS TDS reconciliation. A
  group-by over data that already lands today; no schema change.

**Out of scope v1**: net-taxable-position computation, capital-**loss** set-off
(STCL → STCG+LTCG, LTCL → LTCG only), 8-AY loss carry-forward, and return-filing-date tracking
— all v2 (§Roadmap). v1 reports gains to *reconcile*; it does not optimise tax.

## Data model (high level)

Regenerated from `backend/app/models/` — 15 tables. Every table also carries
`created_at` / `updated_at` via `TimestampMixin` (omitted below except where noted).

```
users (id UUID PK, email, password_hash, display_name)
sessions (id, user_id, family_id, token_hash, expires_at, revoked_at)
  -- the refresh-token store the whole auth contract rests on (ADR-0003); rotation
  -- revokes by family_id, and created_at is the 12h absolute-cap origin
accounts (id, user_id, name, type, issuer, last4, opening_balance_paise, currency,
          parent_account_id, archived_at)
categories (id, user_id, name, kind, is_seeded, archived_at, color, parent_id)
  -- parent_id: nullable self-FK, NULL = a parent row. Depth is capped at 2 (ADR-0012),
  -- enforced in the router. color NULL on a subcategory means "inherit the parent's".
transactions (
  id, user_id, account_id, date, amount_paise, transaction_type, merchant_raw,
  merchant_normalized, category_id, auto_category_id, fingerprint, occurrence,
  source ('import'|'manual'), import_batch_id, confirmed_at, transfer_pair_id
)
  -- occurrence: the F4 multiset ordinal (ADR-0006) — identity lives in the hash,
  -- multiplicity in this column. confirmed_at: NULL = still in the review queue,
  -- so the board and every dashboard filter on it. transfer_pair_id: the F4a link.
merchant_tag_map (id, user_id, merchant_normalized, category_id, hit_count, last_used,
                  pinned)   -- pinned = user-authored, outranks a higher hit_count
merchant_label_map (id, user_id, merchant_normalized, label_id, hit_count, last_used,
                    pinned)  -- F3a Phase-2 auto-learn; composite same-user FK to labels
                             -- + ON DELETE CASCADE
labels (id, user_id, name)   -- F3a user tags; name unique per user
transaction_labels (transaction_id, label_id, user_id)  -- M2M; composite same-user FKs
                                                        -- + ON DELETE CASCADE
import_batches (id, user_id, account_id, source_file_hash, parser_name, imported_count,
                skipped_count, status, error_message)
instruments (id, user_id, symbol, name, asset_class, currency, exchange, current_nav,
             nav_updated_at, archived_at, isin, amfi_code)
investment_transactions (id, user_id, instrument_id, date, transaction_type, units,
                         price_per_unit_native, amount_native_paise,
                         fees_native_paise, fx_rate_to_inr, note,
                         pair_id, import_batch_id, fingerprint, occurrence)
                         -- column is transaction_type, NOT type. pair_id links the two
                         -- legs of one event (IDCW reinvestment dividend+buy; reserved
                         -- for switch_out/switch_in). fingerprint is NULLABLE here
                         -- (manual rows have none) unlike transactions.fingerprint.
fx_rates (id, date, from_currency, to_currency, rate, source)
benchmarks (id, name, kind ('index_fund'), amfi_code, currency, inception_date,
            archived_at)
benchmark_nav (id, benchmark_id, nav_date, nav)   -- column is nav_date, NOT date.
                                                 -- Price cache, backfilled at seed time
```

**Not built yet** (listed here only so the F11 design has a target — do not write queries
against it): `realised_lots (id, user_id, sell_txn_id, buy_txn_id, matched_units,
matched_cost_paise, buy_date, sell_date)`, the FIFO match audit trail. No model, no
migration; `holdings_service._consume_fifo` currently discards the consumed lots. See
§Build sequencing v0.8.

- All amounts stored as `paise` / `cents` (int64) to dodge float precision issues.
- Soft-delete via `archived_at` on accounts / categories. Hard-delete on transactions
  is allowed (user is the only owner). Labels hard-delete too — removal clears their
  `transaction_labels` links (cascade).
- `user_id` present on every owned table. It is a **UUID** (`users.id` is a UUID PK,
  `default=uuid.uuid4`) — there is no integer `1`. The seeded v1 user is the fixed UUID
  `00000000-0000-0000-0000-000000000001`. A data migration or raw aggregate that emits
  `user_id = 1` will not bind. Global reference tables (`fx_rates`, `benchmarks`,
  `benchmark_nav`) deliberately carry no `user_id` — see ADR-0003.
- **No `portfolio_cache` or `balance_snapshots` in v1**: `pyxirr` recomputes
  cold-XIRR in milliseconds for <100 holdings; net-worth is a query-time aggregate.
  Both tables would add complexity (invalidation logic, snapshot scheduling) for no
  observable win at personal scale. Revisit if XIRR computation ever exceeds 200ms.
- **`realised_lots`** persists the FIFO match (F7): one row per buy lot a sell consumes, so
  the F11 capital-gains statement recovers realised gain + holding period per sold lot. A
  table (not denormalized columns on `investment_transactions`) because one partial sell can
  consume N buy lots. `_paise` int64, SQLAlchemy 2.0 declarative — no SQLite-only idioms. No
  FY-report cache table: the statement is a query-time aggregate, consistent with the
  no-snapshot rule above.

## Tech stack

| Layer       | Choice                                                 | Why                                                       |
| ----------- | ------------------------------------------------------ | --------------------------------------------------------- |
| Backend     | **Python 3.13 + FastAPI**                              | User pick. Pydantic v2 for schemas.                       |
| ORM / migr. | **SQLAlchemy 2.0 + Alembic**                           | Type-safe sessions; trivial SQLite → Postgres swap later. |
| Database    | **SQLite (v1)** → Postgres (v2 / hosted)               | Zero ops for personal use; Alembic migrations portable.   |
| PDF parsing | **pdfplumber 0.11.9** (Jan 2026) + **pikepdf 10.6.0** (Mar 2026) | See "PDF parsing library choice" below for rationale.     |
| CSV parsing | stdlib `csv` + per-issuer column map                   | Trivial.                                                  |
| XIRR        | **pyxirr**                                             | Faster + more accurate than home-grown brentq.            |
| FX rates    | **`frankfurter.app`** or **`exchangerate.host`** (free, no key) | Daily INR↔USD rates. Cached in `fx_rates` table; fallback to last-known if API down. |
| Drive sync  | **google-api-python-client** + **google-auth-oauthlib**| Official Google libs. `drive.file` scope = least privilege.|
| Token store | **cryptography (Fernet)** over a local file            | Encrypts the OAuth refresh token at rest.                 |
| Frontend    | **Next.js 16.3.0 LTS (App Router) + React 19 + TypeScript 7.x + Tailwind CSS 4.x** | Portfolio-grade, learning-aligned, real-time-friendly. See "Frontend stack rationale" below. |
| UI kit      | **shadcn/ui** + its `Chart` component (Recharts engine) | Copy-paste components live in your repo — zero runtime dep, no upstream version churn. Production-strength via Radix UI primitives. |
| Data layer  | **TanStack Query v5** (server state) + minimal Zustand only if needed | Polling, cache invalidation, optimistic updates — drives the live-update behaviour in F9. |
| Real-time   | v1: TanStack Query polling. v2: FastAPI **SSE** endpoint + `EventSource` on the client | SSE chosen over WebSocket because portfolio updates are server→client only. |
| API client  | Thin typed `fetch` wrapper (`frontend/lib/api/client.ts`); the consumed schemas are hand-mirrored from the Pydantic models — **~71 exported types**, well past the ~15 originally set as the revisit trigger | Hand-mirroring is **retained by decision**, not because the trigger hasn't fired: codegen would add a build step and a generated-artifact review burden for a single maintainer, and the mirrors have stayed correct where they exist. The cost is stated honestly in the §TypeScript bullet below — a *missing* field is invisible to tsc and must be diffed by hand. Revisit `openapi-typescript` if a second client (mobile, export script) appears. |
| File store  | Local filesystem (v1) → S3-compatible (v2)             | Statement PDFs/CSVs kept for re-parse / audit.            |
| Background  | None in v1 (synchronous imports are fine for personal scale). Later: `arq` or `rq`. |

### Frontend stack rationale (verified May 2026)

Chosen against three constraints simultaneously: (a) **live/reactive dashboards**
(current value, P&L tiles that update without page reload), (b) **portfolio-project
visibility** — this code goes on your GitHub and a recruiter must read it as
production frontend work, and (c) **React as an explicit learning goal**. The
React ecosystem's maintenance cost is real but acceptable when each upgrade
doubles as deliberate practice.

- **Next.js 16.3.0** is the current LTS (Next.js 15 EOLs 2026-10-21). Powers
  vercel.com, notion.so, hulu.com, anthropic.com, OpenAI's chat surface. Turbopack
  is the default bundler in 16 and is stable; React Compiler ships stable too
  (auto-memoisation — fewer manual `useMemo` / `useCallback`).
- **React 19.2** — current stable via Next 16. The Server / Client Components split
  matters; design rule for this app: dashboards and forms are **Client Components**
  (`"use client"`) since they need interactivity / TanStack Query; pages that are
  pure layout stay Server Components.
- **TypeScript 7.x** — industry default. The Pydantic schemas consumed by the frontend
  are hand-mirrored as TS types in `lib/api/client.ts` (see the API-client row above for
  the count and the decision). **Drift is only half-caught by tsc, and this is the
  canonical statement of the asymmetry**: tsc *does* flag a TS field the backend dropped
  or renamed, because a call site reads it. tsc *cannot* see a backend field the TS type
  never declared — no diagnostic anywhere, nothing renders it, and the suite stays green.
  That direction must be diffed by hand when a response schema gains a field; it is how
  `fx_unavailable_count` and `fx_staleness_days` were computed and silently discarded.
- **Tailwind CSS 4.x** — used by GitHub, Shopify, OpenAI, Vercel, Anthropic.
  Native Oxide engine in v4 is significantly faster than v3.
- **shadcn/ui** — *not* an npm package: the CLI scaffolds Radix-UI-primitive-based
  components directly into `frontend/components/ui/`. You own the source. This is
  what makes it production-safe — there is no upstream library to deprecate your
  app, and Radix primitives underneath are the same ones Vercel, Linear, Cal.com,
  and many YC startups ship on. Accessibility tested out of the box (keyboard nav,
  ARIA, focus trapping).
- **shadcn/ui's `Chart` component (Recharts engine)** — mature React chart library
  used widely in finance dashboards. Half the bundle of Tremor for the same
  underlying engine. Tremor was on the table but its team pivoted to copy-paste
  after the Vercel acquisition, so going straight to shadcn/ui Chart is cleaner.
- **TanStack Query v5** — owns all server state. Polls dashboard endpoints, holds
  cache, invalidates on mutations, and (in v2) takes update events off the SSE
  stream. This is the piece that makes the F9 live experience trivial — without
  it you'd be re-implementing the same cache/invalidation logic in `useEffect`s.

**Honest tradeoffs**:

- *Setup cost is real*: a Next.js + TS + Tailwind + shadcn scaffold takes a day
  vs. half a day for HTMX/Jinja. Acceptable given it's a learning investment.
- *Maintenance is real*: even on LTS, expect npm security advisories monthly and
  Next.js minor versions every 2-3 months. The mitigation (see "Maintenance
  posture") is: pin tight, batch upgrades to one weekend a year, treat upgrade as
  practice.
- *Two codebases* (`backend/` Python + `frontend/` TS) instead of one. The shared
  contract is the Pydantic schemas in `app/schemas/`, mirrored by hand in
  `frontend/lib/api/client.ts` (revisit codegen at scale).

### PDF parsing library choice (verified May 2026)

- **pdfplumber 0.11.9**, MIT-licensed, pure-Python on top of `pdfminer.six`, released
  2026-01-05. Best-in-class for *text-based* PDFs with tabular content — and HDFC,
  ICICI, and Axis statements are all text-based (not scanned images), so this is the
  right shape. Critical knobs for financial PDFs: `snap_tolerance` and `join_tolerance`
  on `extract_tables()` — bank statements often have slight column misalignments that
  need tuning per issuer.
- **pikepdf 10.6.0**, MPL-2.0, qpdf-backed, released 2026-03 — used only to decrypt
  password-protected statements before handing the file to pdfplumber. Supports
  AES-256, which is what current Indian banks use.
- **Alternatives considered and rejected**:
  - **PyMuPDF / fitz** — faster, but AGPL-3.0. Fine for personal local use, but it
    poisons a future hosted deployment unless we pay for the commercial license. Not
    worth the tradeoff.
  - **pypdfium2** — fastest, but emits flat text with no table structure. Wrong shape.
  - **Tabula / tabula-py** — requires JVM as a runtime dep. Heavy for personal use.
  - **bankstatementparser** — bundles parsers + local-LLM fallback but is tuned for
    Western banks; Indian formats aren't first-class. Worth re-evaluating in v2 if
    parser maintenance becomes painful.
- **Python 3.13 support**: confirmed for both. pdfplumber's CI explicitly tests
  against 3.10–3.14 (per its README). pikepdf 10.6.0 ships binary wheels for cp310
  through cp314. 3.13 was chosen over 3.14 (latest) to buy ~18 months of ecosystem
  maturity in transitive deps at zero functional cost — matters given the
  one-upgrade-weekend-per-year ceiling.

## Production-grade essentials

Since this codebase is also a portfolio project, the things a reviewer would expect
to see in a serious Python+TypeScript repo are committed scope, not "nice to have":

- **Tests** (`backend/tests/`):
  - `pytest` + `pytest-asyncio` for FastAPI route tests using `httpx.AsyncClient`.
  - Each statement parser has a fixture-based test: a redacted real PDF/CSV in
    `tests/fixtures/<issuer>/` + an expected-output JSON snapshot. New parsers
    can't merge without a fixture.
  - Service-layer tests for `merchant` / `tag_service`, `import_service`, `portfolio_service`
    (especially XIRR cases: empty portfolio, single buy, buy+sell, MF switch).
  - Coverage target: **75%** on backend `services/` and `parsers/`. Not a vanity
    metric for the whole repo — focused where logic actually lives.
- **Linting & types**:
  - Backend: `ruff` (lint + format, replaces black/flake8/isort) + `ty` on `app/`.
    CI fails on either. `ty` has no `--strict` bundle, so ruff's `ANN` rules carry
    annotation completeness — see ADR 0005 for the strictness delta.
  - Frontend: **`tsc --noEmit` only** — that is the whole gate, in CI and in pre-commit.
    `eslint` runs but `eslint.config.mjs` declares **zero rules** (it is an
    ignores-only stub), so `pnpm lint` passes vacuously; `prettier` is installed but
    wired into neither CI nor pre-commit and has no script. Stated plainly rather than
    claiming a gate that is structurally empty: the defect classes tsc cannot see
    (`react-hooks/exhaustive-deps`, a missing `key`) are currently uncaught. Wiring
    eslint-config-next + a `prettier --check` step is scoped work with its own
    verification, not a doc edit — and there are no frontend tests to catch an
    autofix regression.
- **CI** (`.github/workflows/ci.yml`): on push and PR — install, lint, type-check,
  run tests, build the Next.js bundle. Matrix: Python 3.13 + Node 24 (matching `.nvmrc`).
- **Local dev**: `docker-compose.yml` at the repo root, brings up the FastAPI
  backend (SQLite mounted as a volume) + the Next.js dev server. `make dev` runs it.
- **Cloud deploy recipe** (for v1.5): a `fly.toml` (or `railway.json`) that deploys
  the FastAPI app + a managed Postgres + the Next.js frontend as static export.
  Documented in README. Not run in v1, but committed so the path is visible.
- **Structured logging**: `structlog` on the backend with JSON output in non-dev
  mode. Every request logs `request_id`, `user_id`, `route`, `duration_ms`,
  `status`. Import operations log `import_batch_id`, `parser`, `rows_in`,
  `rows_imported`, `rows_skipped`. No PII (PAN, account numbers, card last-4) in
  logs — masking happens at the logger
  (`mask_pii`), never the call site. **Coverage is deliberately narrow, and the
  shape matters**: only PAN and 16-digit cards have free-text regexes
  (`core/pii_patterns.py` — `PAN_RE`, `CARD_RE`), so those are scrubbed wherever
  they appear, including inside exception messages. Account numbers are masked
  **by field name only** — a value passed under a known key becomes `"***"`, but
  a bare account number interpolated into a message or an exception string is
  **not** scrubbed. Pass PII as a structured field; never build it into the event
  string. 13-digit Visa and 15-digit Amex are out of scope, matching
  `scripts/redact_fixture.py`; the two pattern sets are pinned equal by
  `tests/test_fixture_redaction.py::test_redact_script_pattern_parity`.
- **Error tracking** (v1.5): Sentry free tier, FastAPI middleware + Next.js SDK.
  Not v1 because there's no one to page.
- **README**: project pitch in the first 100 lines (screenshots of dashboards,
  quickstart command, architecture diagram). Recruiters won't scroll.

## Critical files (when implementation starts)

- `backend/app/parsers/` — per-source statement parsers implementing a common
  `StatementParser` protocol:
  - `axis_cc.py`, `icici_cc.py` — spending statements (the two shipped). No HDFC and no
    bank-statement parser exists; see §F1 Issuers.
  - `investment_csv.py` — canonical investment-transaction CSV parser (broker / AMC
    exports; `HEADER_ALIASES` column map; standalone, not a `StatementParser`)
  - `indmoney_us.py` — INDmoney US transaction export
- `backend/app/services/fx_service.py` — fetch + cache INR↔USD rates from `frankfurter.app`.
- `backend/app/services/import_service.py` — orchestrates parse → dedupe → auto-tag →
  persist.
- Merchant normalisation + tag lookup/upsert ship as three modules, not one
  `tagging_service.py`: `services/merchant.py` (normalisation — single source of truth),
  `services/tag_service.py` (merchant→category memory), `services/category_service.py`.
- `backend/app/services/portfolio_service.py` — XIRR, allocation, current value.
- `backend/app/services/export_service.py` — produces the CSV zip from the DB.
- `backend/app/services/drive_sync_service.py` — OAuth flow + upload + retention.
- `backend/app/services/backup_import_service.py` — the additive CSV restore (§F10).
- `backend/app/models/` — SQLAlchemy models matching the data-model section.
- `backend/alembic/versions/` — schema migrations.
- `backend/app/api/v1/` — JSON API endpoints consumed by the Next.js client, **flat, one
  module per domain** (`imports.py`, `transactions.py`, `dashboards.py`, …) wired by
  `api/v1/router.py`. There is no `routers/` subpackage; adding one would create a second
  router location the `include_router` wiring doesn't know about, so the endpoint would
  404 with no error anywhere.
- SSE is out of scope (§F9 non-goals) — polling at 30s covers personal scale.
- `frontend/app/` — Next.js App Router pages: `(dashboard)/`, `transactions/`, `accounts/`, `investments/`, `imports/`, `settings/`.
- `frontend/components/ui/` — shadcn/ui copy-pasted components (Button, Card, Table, Dialog, Form, Chart, etc.).
- `frontend/components/dashboard/` — chart components (`SpendByCategory.tsx`, `NetWorthTrend.tsx`, `PortfolioBreakdown.tsx`, `PortfolioTiles.tsx`).
- `frontend/lib/api/client.ts` — thin typed `fetch` wrapper; the consumed schemas are hand-mirrored from the Pydantic models in `backend/app/schemas/` (count + tsc caveat: §Tech stack).
- `frontend/lib/queries/invalidate.ts` — the shared post-mutation cache-invalidation helpers. There are no `use*` query hooks: components call `useQuery` directly with a shared key convention, so `["dashboards"]`-prefixed invalidation is what makes writes refresh reads (§F9).

## Maintenance posture

Target: **one planned upgrade weekend per year**. The React/Next ecosystem makes
this harder than HTMX would, but with discipline it's achievable. Rules:

- **Pin policy**:
  - Backend: `pyproject.toml` uses bounded ranges — floor = current (no regress),
    ceiling = next breaking boundary (minor for 0.x, major for ≥1.x). `uv.lock`
    pins exact versions and is committed. A 3-day `exclude-newer` cooldown
    (`[tool.uv]`) refuses releases younger than 3 days. Ranges only cap
    `uv lock --upgrade`; day-to-day installs (`uv sync`, CI `--locked`) use the lock.
  - Frontend: pin tight — `package.json` with exact versions (`"next": "16.2.10"`,
    not `"^16.2.10"`), `pnpm-lock.yaml` committed. Use `pnpm` (not `npm`) for faster
    installs and stricter dependency resolution.
- **Pick LTS, stay on LTS**: Next.js LTS, Node.js LTS (24.x as of Jul 2026),
  Python 3.13. Don't chase. Skip every minor version that isn't security-relevant.
- **Cap the dep list aggressively**: every npm dependency added is a future
  upgrade-weekend tax. The minimal set: `next`, `react`, `react-dom`, `typescript`,
  `tailwindcss`, `@tanstack/react-query`, `zod` (for client-side validation),
  generated API client deps. shadcn/ui adds *zero* deps beyond Radix primitives.
- **Annual upgrade ritual** (one weekend per year, typically Q1):
  1. Read Next.js, React, Tailwind upgrade guides.
  2. `pnpm update --latest` to LTS targets; `uv lock --upgrade` for Python.
  3. Run E2E smoke test from the Verification section.
  4. Commit lockfile. Done.
- **What we deliberately skip**: Dependabot / Renovate auto-PRs (they spam),
  weekly npm-audit panic, framework migrations between annual cycles.
- **Off-cycle upgrades**: in-range backend re-locks (`uv lock --upgrade`) are
  low-risk anytime — bounded by the range ceilings, gated by the 3-day cooldown,
  validated by the test suite. Crossing a ceiling (breaking major / framework
  migration) stays an annual-weekend or CVE-driven event. v1 is `localhost` only —
  no exposure. v1.5 adds basic auth before any cloud deploy.

## Build sequencing — what to build first

The PRD lists ~10 features. Trying to build all of them in parallel is how
side-projects die. The highest-risk unknown is **whether real Axis/ICICI PDFs
parse cleanly** — every downstream feature (dedup, auto-tag, dashboards) is
gated on imports working at all.

**Recommended order (each milestone is mergeable, demoable, and survives if the
next milestone never ships):**

- **v0.1 — one issuer, end-to-end.** *(Shipped: Axis CC was the first parser, then
  ICICI CC. HDFC was dropped — the card isn't held, so no redacted fixture can exist.)*
  Build: backend models, the CC PDF parser, `import_service` (with dedup +
  fingerprint), merchant normalisation + tag memory (exact match), and a single Next.js page that
  uploads a PDF and shows the resulting table with editable category dropdowns.
  *No dashboards. No investments. No Drive sync.* Goal: prove the import → tag
  flow works on your real statements. Maybe 1 weekend of work; de-risks 80% of
  the project.
- **v0.2 — second + third issuer.** Add ICICI and Axis parsers. Validate the
  `StatementParser` abstraction holds up across formats. If it bends, refactor
  now while there are only 3 parsers.
- **v0.3 — manual transactions + categories UI.** Forms for F2 + F5. Now the
  daily-driver loop works end-to-end without imports.
- **v0.4 — investment side: manual + CSV import.** Add `instruments`,
  `investment_transactions`, the FIFO/dividend/split rules, and the canonical
  investment-CSV importer. XIRR computation. No dashboards yet — just a holdings table.
- **v0.5 — multi-currency + INDmoney.** FX rate service, USD support, INDmoney
  parser. Test against your real INDmoney export.
- **v0.6 — core dashboards (4 of the 5 v1 views).** Live tiles + monthly-by-category +
  weekly/monthly bar + the scalar net-worth headline. TanStack Query polling.
  (The 5th v1 view, portfolio-vs-benchmark, lands in v0.6.5 — it needs the NAV snapshot first.)
- **v0.6.5 — portfolio vs benchmark (scalar alpha).** Add `instruments.isin` / `amfi_code`,
  the manual-trigger NAV snapshot (AMFI NAVAll for MFs + a public Indian-equity quote
  source), the `benchmarks` / `benchmark_nav` tables
  (seed-time NAV backfill), and `GET /portfolio/performance` → KPI tiles on the portfolio
  page. The "am I beating the market" number; the moving line chart stays deferred (Roadmap).
  (Promotes `httpx` to a prod dep here, ahead of its v0.5 FX use.)
- **v0.7 — export + Drive sync.** CSV export, Drive OAuth, manual sync.
- **v0.8 — tax statements & reporting (F11).** Persist the FIFO lot-match (extend
  `holdings_service._consume_fifo` to *emit* the consumed lots into `realised_lots` — today it
  discards them), add realised-gain classification (STCG / LTCG / debt-slab), and build the
  capital-gains statement + dividend-income summary endpoints and pages. Depends on the F7
  investment lots (v0.4). *No loss set-off, no carry-forward* — that's the v2 optimiser
  (Roadmap). Goal: a statement you can reconcile against your AMC CG report + AIS at filing.
- **v1.0 — production polish.** README screenshots, all tests passing, CI green,
  Docker Compose works, deploy recipe documented. Tag and ship.

**Anti-goals during sequencing:**
- Don't build dashboards before you have real data flowing in.
- Don't build the v2 SSE push before v1 polling is proven.
- Don't perfect the auto-tag normaliser regex before you have 500 real merchants
  to test against.

## Roadmap

- **v1 (this PRD)** — everything above.
- **v1.5** — basic auth, deployable to a cloud box.
- **v2** — OCR for scanned bills; recurring-transaction detection; budgets & alerts;
  capital-gains **loss set-off** (STCL → STCG+LTCG, LTCL → LTCG) + 8-AY carry-forward +
  net-taxable computation (the F11 statement reports gains only in v1); a
  **tax-loss-harvesting** view (needs the per-lot override deferred in F7 + per-lot
  *unrealised* gain, currently aggregate-only);
  multi-user + **household / family net-worth roll-up** (self-host; aggregate-only
  visibility — see Users & access); in-app restore-from-Drive UI; scheduled / auto Drive sync; encrypted
  backups; full SQLite-file backup option; **scheduled / live** NAV refresh + US-equity
  NAV feeds (Yahoo Finance / Polygon) + INR↔USD FX — v1 already ships a *manual-trigger*
  snapshot for Indian NAVs/prices (AMFI NAVAll for MFs + a public Indian-equity quote
  source; free; see F7/F8/F9), so v2 only adds the scheduler and the US-equity price source
  (+ FX); the **moving portfolio-vs-benchmark line chart** + up-to-3
  overlay (needs dense per-holding historical NAV; v1 ships the scalar alpha only);
  additional currencies (GBP / EUR); **SSE push channel** for live portfolio updates
  (replacing v1's polling — see F9).
- **Considered, unscheduled** — moderate adds whose backing data mostly already ships, parked
  here so the decisions aren't lost. None block v1. Most came out of the two competitive
  passes written up in
  [docs/research/competitive-findings.md](docs/research/competitive-findings.md) (2026-07-30
  and the 2026-08-10 addendum) — read its **§7 Killed** table before proposing anything
  adjacent, since several nearby ideas were rejected there with cited evidence and should not
  be re-litigated without new evidence.
  - **Split transactions** — one statement line across N categories (a Flipkart order that is
    part electronics, part household; a Swiggy Instamart order that is part groceries, part
    eating out). All four surveyed peers ship it; we carry a single `category_id`. Take
    Actual Budget's parent/child shape — children sum to the parent, parent keeps the
    identity columns — rather than Firefly III's transaction-group model, which moves
    identity off the row and collides with [ADR-0006](docs/adr/0006-f4-dedup-key.md). It is
    also the only dedup-safe way to split: today's delete-and-re-enter workaround discards
    `origin_fingerprint`, so re-importing that statement re-stages the original line
    ([ADR-0007](docs/adr/0007-transaction-field-editability.md) rule 9). The work is a
    parent-exclusion predicate applied consistently across every §F8 aggregate — miss one and
    it double-counts (research §13.3).
  - **Per-category exclude-from-totals** — **not being built** (decided 2026-08-12). Lunch
    Money's category flag would stop a SIP debit counting as consumption across every spend
    aggregate while it also counts as a holding under §F7 (`NET_WORTH_EXCLUDED_TYPES` is
    account-typed and does not reach it). The chosen answer instead is a workflow one:
    **investment-transfer rows are discarded at import review rather than committed**, so they
    never reach an aggregate. That relocates the error rather than removing it — a discarded
    debit never reduces the account balance, so **net worth is overstated by the cumulative
    discarded amount and compounds monthly** — and discarded rows re-stage on re-import
    (ADR-0006's re-surface contract). Revisit the flag if the accounts panel or net-worth
    figure starts being relied on. Full trade-off table: research §13.4.1.
  - **Target-allocation drift** — the allocation donut already computes actuals; net-new is
    storing a user target + the drift delta + the rupee move to rebalance.
  - **Spend-spike anomaly** in the review queue — the untagged-import queue already ships;
    this flags a merchant whose amount jumps vs its trailing 3-month average.

## Verification

End-to-end test once built:

1. **Import**: Upload a real Axis or ICICI CC PDF statement (the two shipped parsers) →
   confirm transactions appear in the review screen with parsed dates, amounts,
   merchants. Upload the same file again → confirm "Imported 0, skipped N" dedupe summary.
   - **Credit-side classification**: a `CHARGEBACK` credit row must store
     `transaction_type = spend` with a **positive** `amount_paise` — a refund is a signed
     spend, not its own type ([ADR-0009](docs/adr/0009-refund-as-signed-spend.md)) — so it
     nets against spend per F4a-3, not `income`. Both parsers' refund vocabularies must agree
     on it — they diverged once. Netting assertion: importing that row alongside its original
     negative spend in the same category must bring the category's signed total to **exactly
     zero**, not merely "close" or "smaller."
   - **Card-bill payment is reachable in review**: a statement containing a `PAYMENT RECEIVED`
     row surfaces it with `cc_payment_candidate = true`; choosing *Card bill payment* stages it
     with `category_id` still NULL, and committing it alongside an imported bank debit of equal
     magnitude within ±2 days flips both rows to `transfer` with a shared `transfer_pair_id`
     (F4a-1). With no matching debit it commits as uncategorized `income` and **never** appears
     in an income category total.
   - **Column detection on a serial-numbered table**: a statement whose rows carry a leading
     serial-number column (`1`, `2`, …) parses with the real amount, not `₹1` — the amount
     column is detected *after* the date column, never from index 0, since a bare digit string
     is a valid amount. Getting this wrong poisons the F4 fingerprint, the F3 tag-map key and
     every spend total at once.
   - **Archived account refused**: archive a credit card, then `POST /imports` with that
     `account_id` → refused with zero staged rows and no import batch; the same upload on an
     active card still succeeds. Statement import must agree with the four sibling account
     pre-flights (manual entry, both transfer legs, parent-account link), which all refuse an
     archived account — otherwise a stale client cache can commit rows onto an account
     `GET /accounts` will never return.
   - **Same-day duplicates (F4 / [ADR-0006](docs/adr/0006-f4-dedup-key.md))**: a statement
     containing two genuinely-distinct rows that share date + amount + merchant (two auto
     rides at the same fare) imports as **2** transactions, not 1, with `occurrence` 0 and 1.
     Re-uploading that file stages 0. Deleting one of the pair and re-uploading re-stages
     exactly one row (the documented re-surface contract), never two. All three import
     paths (statement, investment CSV, backup restore) share one allocator and must show
     this same behaviour — they had three copies of it, and the copies drifted once.
   - **The pending feed never labels a batch with another user's account**
     ([ADR-0003](docs/adr/0003-multi-user-auth.md)): `import_batches.account_id` is a
     plain FK, so store a batch of your own pointing at another user's account →
     `GET /imports/pending` shows the batch with a **null** name and last4, never theirs.
     A batch with no account at all (backup restore) still appears in the feed — the
     user predicate belongs in the join's ON clause, not the WHERE.
   - **All three import paths emit the telemetry this document promises** (§Production-grade
     essentials): a statement upload, an investment CSV and a backup restore each log one
     `import_completed` event carrying `import_batch_id` / `parser` / `rows_in` /
     `rows_imported` / `rows_skipped`, and none carries a merchant, PAN, account number or
     card last-4. Only the statement importer did until 2026-08-02. **The counts are not
     interchangeable across the three** and the field set alone does not make them so:
     `rows_in` is parser output for statements, post-parse rows for the investment CSV, and
     `transactions.csv` rows only for a restore (accounts/categories are counted separately);
     `rows_skipped` bundles zero-paise rows with duplicates for statements and means
     duplicates only for the other two. What IS uniform, and is the thing to assert, is
     `rows_in == rows_imported + rows_skipped + rows_rejected` on every path — including a
     re-upload, where the investment importer's hash short-circuit reports every row skipped
     rather than reporting nothing at all.
   - **Closing-balance reconciliation** (F4a case 5, [ADR-0010](docs/adr/0010-parsed-statement-return.md)):
     import a CC statement whose printed closing balance agrees with its rows → the batch
     reconciles (delta `0`) and no banner shows. Delete one row's page from the same statement
     and re-import into a fresh account → the review screen warns with the exact missing
     amount, **the commit still succeeds**, and the delta persists on the batch after commit. A
     statement with no summary block (the Axis Flipkart layout) imports with the check simply
     not run — never a parse error. A credit-card "Total Amount Due" of ₹X stores as **−X
     paise**: owed is negative, matching `opening_balance_paise` for a card.
2. **Auto-tag**: Tag 5 transactions with categories → upload next month's statement →
   confirm same merchants are pre-categorised.
   - **Prefill never crosses users** ([ADR-0003](docs/adr/0003-multi-user-auth.md)):
     `merchant_tag_map.category_id` is a plain FK, so store a map row of your own
     pointing at another user's category → it is absent from the prefill map, and no
     imported row is stamped with that `auto_category_id`. This is the one
     merchant-memory read whose result gets written back onto data.
3. **Manual entry**: Add a cash spend → verify it appears in monthly spend by category.
4. **Investments**:
   - **Indian MFs / equities via CSV**: import a broker / AMC transaction CSV → confirm
     each `symbol` appears as an `instrument` and every buy / sip / dividend lands as an
     `investment_transactions` row. Re-import the same file → confirm "already imported"
     (0 new). Spot-check totals against the broker statement.
   - **Sign discipline at the CSV boundary**: a row carrying a negative `fees` (or negative
     `units`) is rejected with a line-numbered, PII-safe reason and imports 0 rows — the
     same body posted to `POST /investment-transactions` is a 422, and invested totals plus
     portfolio XIRR are unchanged by that row's presence in the file.
   - **US stocks via INDmoney**: import an INDmoney transaction export → confirm
     tickers appear with `currency = USD`, transactions have `fx_rate_to_inr`
     stamped from the date's FX rate, and current INR-value rolls up correctly.
   - **XIRR**: pick one MF holding with ≥6 months of history → verify computed XIRR
     matches an external calculator (groww / kuvera / online XIRR tool) to within
     0.1%.
   - **IDCW reinvestment**: record one via `POST /investment-transactions/reinvestment`
     → both legs appear on the same date sharing a `pair_id` in both directions, and
     holdings units + invested match the AMC statement (not the dividend-only figures).
     Sell exactly the funding lot → the reinvest lot survives at *its own* cost, proving
     it opened a real FIFO lot rather than blending into the original. XIRR stays within
     0.1% of an external calculator, since the pair nets to zero. Delete either leg →
     the survivor's `pair_id` is NULL and the other row is untouched.
   - **NAV / price snapshot**: trigger the snapshot (the sync icon on /portfolio or
     /holdings) →
     a held Indian MF's `current_nav` + `nav_updated_at` update from AMFI NAVAll (matched by
     `isin`) and a held Indian equity's price updates from the public quote source (by
     `symbol` / `isin`); the response reports `catalogue_staleness_days` and
     `null_nav_count` (no silently stale valuation). That number covers every active
     priced instrument, exited ones included — `GET /portfolio/performance`'s
     `nav_staleness_days` is the same arithmetic over what you still hold, so the two
     legitimately differ and are named apart.
   - **A hand-priced NAV is dated, not timestamped**: register an `fd` instrument with a
     `current_nav` and `nav_as_of` = today − 90 → `GET /portfolio/performance` reports
     `nav_staleness_days == 90` and the page renders the stale-valuation caveat. Re-`PATCH`
     the same price with a corrected `nav_as_of` → the stamp moves even though the price
     did not. `nav_as_of` in the future, or without a `current_nav`, is a 422; clearing the
     price clears the date. (Before this, the manual path stamped write time, so the answer
     was 0 and no caveat showed — for exactly the asset classes no refresh can fix.)
   - **Portfolio vs benchmark (scalar alpha)**: with a seeded portfolio, `GET
     /portfolio/performance` returns portfolio XIRR, the chosen index fund's benchmark XIRR
     (your cashflows replayed in), the alpha (% points), and the rupee gap. The benchmark
     XIRR matches a hand-computed cashflow-matched counterfactual to within 0.1%. When
     `nav_updated_at` lags the benchmark's as-of date, the response flags staleness rather
     than fabricating alpha.
5. **Dashboards**: With ≥3 months of data, the shipped dashboard views (live tiles,
   monthly-by-category, weekly/monthly bar, the scalar net-worth headline,
   portfolio-vs-benchmark) render without errors and the spend / net-worth totals
   reconcile against raw transaction sums (ad-hoc SQL check); the benchmark alpha is
   verified per step 4. Confirm the account types in scope (view 4): net worth sums
   **`bank` and `cash` only**, and **excludes** both `credit_card` (a card's spend
   must not read as debt) and `investment` (a placeholder — its value is already
   counted as the portfolio, so adding both double-counts). Drive the second one
   end-to-end: create an investment account (its opening balance is rejected as
   non-zero, so it starts at ₹0), import a CSV of holdings worth a known amount, and
   check `GET /dashboards/overview` reports that amount exactly once — then confirm
   /dashboard's Assets · Investments · Owed line still sums to the headline. Not
   built, so not checked here: the net-worth-over-time trend and a savings-rate tile.
6. **Reconciliation rules** (F4a):
   - **CC-bill double-count**: no bank-statement parser exists for any issuer, so this
     is exercised via **manual entry** rather than a second import. **Order matters —
     the bank leg must exist first.** `auto_link_cc_bill` runs from exactly one place,
     the import batch-commit endpoint, and never on `POST /transactions`, so a bank-side
     row added *after* the commit will not retro-link. Steps: in /settings/accounts
     create a bank account and a CC, then edit the CC and set **"Paid from"** to that
     bank (the F4a rule-1 link gate — without it the reconciler returns early and the
     payment stays `income`) → add the matching bank-side debit by hand (F2) on that bank
     for the bill amount, dated D, and confirm it → import a CC statement containing a
     `PAYMENT RECEIVED` credit dated D → **commit the review queue** → confirm the two
     rows are auto-linked as `transfer` (sharing `transfer_pair_id`), are excluded from
     monthly spend, and that `GET /dashboards/overview` `income_paise` for that month no
     longer includes the bill. Re-check when a bank parser lands.
   - **Symbol-keyed instrument identity**: create a manual `instruments` row for a
     symbol you already hold → import a CSV containing that symbol → confirm the rows
     attach to the existing instrument (no duplicate, no merge prompt).
7. **Export + restore**: Click "Download backup" — the zip opens and contains exactly
   `metadata.json`, `accounts.csv`, `categories.csv`, `transactions.csv`, each with rows,
   and their row counts match `SELECT COUNT(*)` on those three tables. (Investments are
   deliberately not in the zip — §F10.) Then "Load backup" the same zip back →
   confirm it is additive: 0 new transactions, existing rows skipped by recomputed
   fingerprint, nothing wiped, and any `transfer_pair_id` links survive.
   - **Hand-edited zip**: change one `transactions.csv` `merchant_normalized` cell to mixed
     case before "Load backup" → the restored row still dedups against its natively-imported
     twin (Imported 0), the stored value is lowercase, and the merchant still auto-tags on the
     next import. A hand-edited file is this parser's declared threat model, so a capitalised
     cell must not fork the F4 identity.
   - **Hand-edited UTC offset**: a `confirmed_at` cell carrying `+05:30` restores as the same
     instant as its UTC spelling, on both dialects — and a cell with **no** offset restores
     unshifted, since that is the shape the export itself writes. This is the one datetime
     boundary that accepts hand-authored input, and the export gives no hint UTC is required;
     an un-normalized offset was stored 5h30m wrong on SQLite, permanently and silently.
8. **Drive sync**: Complete the one-time OAuth flow → click "Sync to Drive now" →
   confirm the zip appears in the `FinTracker/` folder in Drive. Click sync again →
   confirm a new file is created and (after N+1 syncs) the oldest is pruned.
9. **Live updates**: Open the dashboard in one browser tab. In another tab, save a
   new investment buy / update an instrument's NAV. Within ≤1 second the first tab's
   portfolio tiles should reflect the change (via TanStack Query mutation invalidation).
   With both tabs open and idle, confirm tiles refresh every ~30 seconds without user
   action. Open the React Query devtools to see the queries refetching.
10. **Capital-gains statement (F11)**: seed sells that straddle the 12-month equity boundary,
    one debt fund bought after 2023-04-01, and one lot with bonus units → the statement buckets
    each sale correctly (STCG / LTCG / debt-slab), surfaces the ₹1.25 L equity-LTCG exemption
    line, and per-bucket subtotals reconcile against a hand-computed FIFO calc. Re-running over
    the same data is stable (idempotent).
11. **Dividend FY summary (F11)**: dividend rows roll up per financial year; the total matches a
    raw `SELECT SUM(amount_native_paise) WHERE transaction_type='dividend'` over that FY's date
    range.
12. **Schema parity** (`tests/test_migration_parity.py`, the only place app schema meets the
    *migrated* DB — every other test builds it with `create_all`): the parity test fails on a
    seeded enum-vocabulary divergence (add a member to a model `Enum` without its Alembic
    CHECK), on a seeded default divergence (drop a `server_default` the migration declares),
    and on a duplicate per-table CHECK name (two columns sharing one `Enum(name=...)`). All
    three are silent on SQLite and fatal on Postgres or on the user's real migrated DB, so a
    green suite is not evidence without them.
13. **Absolute session cap, dialect-honestly** (ADR-0001 rule 5): back-date a refresh family's
    `sessions.created_at` via SQL past `session_absolute_ttl_hours` → the next refresh revokes
    the whole family and returns 401, **without patching any application symbol**. Separately,
    pinning the app clock to a fixed instant and inserting a row must make `created_at`,
    `updated_at` and both merchant maps' `last_used` read back as *that* instant — proving the
    app clock wrote them, not the database server's. On SQLite the two clocks are
    indistinguishable in value, so this is the only check that fails before the fix rather than
    at the Postgres cutover, where a non-UTC server `TimeZone` moves the cap by its offset.
    - **Same numbers on both deployments**: with the host TZ set to `Asia/Kolkata`, at 02:00
      local, `GET /portfolio/summary`, `GET /holdings` and `GET /dashboards/overview` report
      the same current value / net worth as the Docker UTC stack for the same data. The two
      shipped deployments previously disagreed for 5.5 hours a day: routes read the host's
      local date, and the "now" FX reads carried it into a money number. A "now" view uses
      `latest_rate` (no date at all); only genuine as-of anchors take `clock.today()`.
14. **Demo login is closed by default** ([ADR-0003](docs/adr/0003-multi-user-auth.md) §Demo
    account gate): with `DEMO_LOGIN_ENABLED` unset, `POST /auth/login` with the demo
    credentials returns **401** *and* `GET /auth/config` reports `demo_login_enabled=false`
    — **on plain http**, i.e. with `COOKIE_SECURE` at its default. Set it to `true` and the
    demo login works; set it to `true` alongside `COOKIE_SECURE=true` and the login stays
    refused while the backend logs `demo_login_enabled_but_inert` at boot. Then bring up
    `make up-proxy` and request `GET /auth/config` from another device on the LAN: it must
    report `false`. Both halves matter — the transport signal alone cannot close this,
    because Caddy serves `:80` and the gate rode `cookie_secure` until 2026-08-02. (Note
    the login page caches this response with `staleTime: Infinity`, so an already-open tab
    keeps its button until reload.)
15. **Merchant alias layer + seed dictionary** ([ADR-0011](docs/adr/0011-merchant-alias-layer.md)):
    register a fresh user → their `merchant_alias` / `merchant_tag_map` rows carry the seeded
    dictionary (`is_seeded=True`, `hit_count=0`) → import a statement containing a seeded merchant
    under a raw descriptor never seen before → the row prefills its category with no prior
    hand-tag. A second, differently-referenced descriptor for that same real merchant (e.g. a
    different order-id suffix) also prefills without a second hand-tag, via the same alias.
    `coverage_rate` rises against the Phase A0 baseline, and every `transactions.fingerprint` is
    byte-identical to the value produced with the alias table empty (the
    [ADR-0006](docs/adr/0006-f4-dedup-key.md) guard).
    - **Authoring leg**: the seed dictionary is a starting point, not the whole story, so the
      user must be able to correct it. Author a *narrowing* alias for a brand the dictionary
      already seeds — `uber eats -> uber eats` against the seeded `uber -> uber` — and confirm
      `POST /rules/aliases` accepts it (a seeded canonical is also a seeded pattern, and an
      over-broad conflict check made this exact submission 422 once), then that the sub-brand
      resolves to its own canonical while the parent brand does not move. The two brands must
      then learn independently: confirming a category on one must not change the other's
      suggestion.
16. **Two-level categories** ([ADR-0012](docs/adr/0012-category-hierarchy.md)): register a
    fresh user → `GET /categories?tree=true` returns 10 parents, every seeded subcategory
    hangs off one of them with `color = null`, and no row is three deep. Create a
    subcategory under a parent of the *other* `kind` → 422; under a subcategory → 422;
    under another user's parent → 422 (not 404 — the parent is a body FK, ADR-0003 rule 3).
    - **Reparent is not a delete.** PATCH a subcategory to `parent_id: null` → it becomes a
      root and **its transactions still resolve**. This is the path an ORM
      `delete-orphan` cascade silently turns into a row deletion, so assert the
      transaction count, not just the category's `parent_id`.
    - **Archive cascades one level, and only forward.** Archive a parent → it and its
      active children carry the same `archived_at`, transactions keep resolving their
      (now archived) category, and the tree view omits the whole family. Archive a child →
      the parent is untouched. Stored `archived_at` is **naive UTC** (ADR-0001 rule 5) on
      every row the cascade writes, parent and children alike.
    - **Migration parity, both directions.** `alembic upgrade head` on a database seeded
      with the pre-0033 flat defaults produces the *same* `(name, kind, parent_id IS NOT
      NULL, color)` set as `provision_default_categories` does for a fresh registrant —
      a migrated user and a new user must not render the same taxonomy in different
      colours. Then tag a transaction to a seeded subcategory and `alembic downgrade 0033`
      → the downgrade must not orphan or delete that transaction's category, and must not
      clear `parent_id` on a hierarchy the *user* authored.
    - **Rollup arithmetic.** With a parent holding direct spend *and* two children, one of
      which has a refund: the F8 parent bar, the subcategory drilldown (including the
      parent's own direct spend as its own row), and the transaction list reached by
      clicking through all agree on the same total, and the drilldown's shares **sum to
      100%**. A single share may legitimately exceed 100% when a refunding sibling offsets
      it — that is the honest number and it is displayed, but the rendered *bar* is clamped
      to `[0, 100]` so it never overflows its track. What must never appear is a negative
      rupee headline when a parent nets positive.
    - **Backup round-trip.** Export with a parent/child pair → restore into a fresh
      database → the child's `parent_id` points at the restored parent. Hand-edit the zip
      so a child's `parent_name` names a category that isn't in the file → the restore
      still succeeds, but the flattened row is reported in `warnings`, never silently.

## Open assumptions to confirm later

- v1 currencies: **INR + USD only**. Other currencies (GBP / EUR / SGD) would
  need adding to the FX layer + UI; defer until you actually hold one.
- PDF statement passwords vary by bank and by document type. We prompt per-upload
  rather than store them.
- INDmoney's export format hasn't been formally documented — first import will
  involve sample-fitting the parser against your actual export. Worst case it's a
  CSV with a slightly different column order than expected; trivial to adjust.
- SQLite is acceptable for v1 even at a few years of personal data (~10–50k rows).
  Confirmed: well within SQLite's comfort zone.
- FX rate at transaction date: for INR investment rows (the v1 CSV path) the rate doesn't matter
  (everything is INR). For INDmoney rows we stamp the date's official RBI / market
  rate via `frankfurter.app`. If you'd prefer to use INDmoney's *own* booked rate
  (which includes their FX markup), the parser can be tuned to extract it from the
  export. Default: market rate. An **FX-markup tracker** (cumulative ₹ lost to INDmoney's
  spread = booked − market rate, summed over US buys) would build on extracting that booked
  rate — but it is gated behind the whole F7 USD/FX milestone (v1 is INR-only; the CSV parser
  rejects non-INR), so it is a v2-adjacent insight, not a v1 add.
