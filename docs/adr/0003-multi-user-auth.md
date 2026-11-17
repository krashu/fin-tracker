# ADR-0003: Cookie-JWT auth with rotating refresh; every owned row is scoped to one user

**Status**: Accepted
**Date**: 2026-07-18 (decided) · **Revised**: 2026-11-15
**Scope**: PRD §Users & access, §Non-goals, §Data model · `app/api/deps.py` (`get_current_user_id`),
`app/api/v1/auth.py`, `app/services/auth_service.py`, `app/services/guest_service.py`, `app/services/user_teardown.py`,
`app/core/security.py`, `app/core/config.py` (`Settings`), `app/core/rate_limit.py`,
`app/core/log_config.py`, `app/middleware.py` (`OriginCSRFMiddleware`), `app/models/session.py`
**Supersedes / Related**: ADR-0001 (portability rules out RLS), ADR-0002 (composite same-user FK)

## Decision

A [user](../../GLOSSARY.md#users--tenancy) authenticates with a short-lived httpOnly-cookie access JWT
plus a hashed, rotating, family-revocable refresh token. Every [owned table](../../GLOSSARY.md#users--tenancy)
carries `user_id`; every read restates it, a single-row miss is 404, a cross-user reference is 422 at
the boundary and unstorable in the DB. `benchmarks`, `benchmark_nav` and `fx_rates` are the only
global tables.

## Rules

**Tokens and sessions**

1. **Access token** is an HS256 JWT (default 15 min) sent only as an httpOnly `SameSite` cookie, never
   in a body; every route resolves it through `get_current_user_id`, and a missing, bad or expired
   token is 401. — *Enforced by*: service · *Locked by*: `tests/api/test_auth.py::test_me_requires_auth`,
   `::test_expired_access_token_401`
2. **Refresh token** is opaque, stored only as a hash in `sessions`, cookie-scoped to `/api/v1/auth`.
   Each refresh rotates single-use within its [refresh family](../../GLOSSARY.md#users--tenancy);
   presenting a revoked token revokes the whole family. — *Enforced by*: service ·
   *Locked by*: `tests/api/test_auth.py::test_refresh_rotates_token`, `::test_refresh_reuse_revokes_family`
3. **A family dies after 30 minutes idle or 12 hours from login**, whichever is first
   (`SESSION_IDLE_TIMEOUT_MINUTES`, `SESSION_ABSOLUTE_TTL_HOURS`). — *Enforced by*: service ·
   *Locked by*: `tests/services/test_auth_service.py::test_rotate_rejects_past_absolute_cap`,
   `::test_rotate_rejects_past_idle_timeout`, `::test_idle_timeout_measures_from_last_refresh_not_login`
4. **A token for a user that no longer exists is 401**, never a 500 from a later FK failure. —
   *Enforced by*: service (`get_current_user_id` confirms the row) ·
   *Locked by*: `tests/api/test_auth_guest.py::test_pruned_guest_token_is_401`, `::test_expired_guest_token_is_401`
5. **CSRF**: every unsafe request carries an `Origin` from the explicit allowlist (missing is 403);
   credentialed CORS uses the same list, and a wildcard is refused at startup. `SameSite=none` needs
   `Secure`; the default `JWT_SECRET` is refused when `COOKIE_SECURE`. — *Enforced by*: service +
   config validation · *Locked by*: `tests/api/test_auth.py::test_csrf_missing_origin_rejected`,
   `tests/test_config.py::test_cors_wildcard_rejected`, `::test_default_jwt_secret_rejected_when_secure`
6. **Passwords are argon2id**; an unknown email, a password-less user and a gated demo login all run a
   dummy verify. — *Enforced by*: service ·
   *Locked by*: `tests/services/test_auth_service.py::test_failed_logins_all_run_a_dummy_verify`

**Tenant isolation**

7. **Every read of an owned table restates `user_id = :uid`**, joined tables and re-fetches by
   already-scoped ids included. The only exemptions are the identity lookups: `sessions` by
   `token_hash`, `users` by id or email. — *Enforced by*: service ·
   *Locked by*: `tests/api/test_imports_review.py::test_pending_never_labels_a_batch_with_another_users_account`
8. **Single-row fetch is `WHERE id = :id AND user_id = :uid`; a miss is 404**, never 403 or 200. —
   *Enforced by*: service · *Locked by*: `tests/api/test_auth_isolation.py::test_accounts_are_isolated_per_user`
9. **A body FK naming another user's row is 422** (`account_id`, `category_id`, `instrument_id`,
   `parent_account_id`, category `parent_id`, label, transfer leg). — *Enforced by*: service ·
   *Locked by*: `tests/api/test_transactions.py::test_post_foreign_user_account_422`
10. **Every owned→owned FK is a composite same-user FK** `(fk, user_id) → (id, user_id)` over an
    `(id, user_id)` unique index, so a cross-user link cannot be stored even if rule 9 is skipped. —
    *Enforced by*: DB · *Locked by*: `tests/api/test_rules.py::test_cross_tenant_label_rule_rejected_by_composite_fk`;
    a metadata test over every owned FK (planned)
11. **Every owned table has a row in the central matrix** `tests/api/test_auth_isolation.py`, and guest
    teardown empties every owned table. — *Enforced by*: tooling · *Locked by*:
    `tests/models/test_owned_tables.py::test_every_non_global_table_has_user_id`,
    `::test_every_owned_table_is_torn_down`

**Accounts, registration and demo**

12. **The first user can always register; after that, registration is open only when
    `REGISTRATION_ENABLED` (default false).** Registration provisions the default categories and the
    [seed dictionary](../../GLOSSARY.md#transactions--import); a duplicate email is 409. — *Enforced by*:
    service · *Locked by*: `tests/api/test_auth.py::test_register_refused_when_registration_disabled`,
    `::test_first_user_registers_when_registration_disabled`, `::test_register_duplicate_email_409`,
    `tests/test_config.py::test_registration_closed_by_default`
13. **Change-password** re-hashes, revokes every family and issues a fresh one to the caller; the
    demo account and guests get 403. — *Enforced by*: service ·
    *Locked by*: `tests/api/test_auth.py::test_change_password_signs_out_other_sessions_keeps_current`,
    `::test_change_password_refused_for_demo_account`
13b. **Delete my account** (`POST /auth/delete-account`) erases the caller immediately: it requires the
    current password and the typed confirmation phrase `delete my account`, then hard-deletes every
    owned row through the same teardown as guest expiry (`teardown_user`), in one transaction, and
    clears the auth cookies. A wrong password is 400 and deletes nothing: the session is valid, so it
    is a body error, never a 401. There is no grace period and no undo; the UI offers a backup
    download first. The demo account and guests get 403. The client reads
    `UserRead.can_manage_credentials` (false for the demo account and guests) to show a notice instead
    of the form. Deleting the last user reopens first-user registration (rule 12). — *Enforced by*:
    service · *Locked by*:
    `tests/api/test_auth.py::test_delete_account_erases_every_owned_row`,
    `::test_delete_account_wrong_password_400`, `::test_delete_account_refused_for_demo_and_guest`,
    `tests/api/test_auth.py::test_me_says_demo_and_guest_cannot_manage_credentials`,
    `frontend/app/settings/security/delete-account-card.test.tsx` ("posts the password and the phrase,
    then signs the session out")
14. **The shared [demo account](../../GLOSSARY.md#users--tenancy) logs in only when `DEMO_LOGIN_ENABLED`
    and not `COOKIE_SECURE`** — never over HTTPS. `Settings.demo_login_permitted` is the one expression
    the login path and `GET /auth/config` read. — *Enforced by*: config + service ·
    *Locked by*: `tests/api/test_auth.py::test_demo_login_refused_by_default_and_config_agrees`,
    `::test_auth_config_demo_disabled_when_cookie_secure`, `::test_demo_login_refused_over_https_whatever_the_flags`
15. **[Guest sandboxes](../../GLOSSARY.md#users--tenancy) have their own gate** (`GUEST_SANDBOX_ENABLED`,
    default off) and may run over HTTPS. A guest is password-less, expires (default 2h), cannot upload
    files (imports offer bundled redacted sample statements), and is pruned with every owned row.
    `MAX_GUEST_ACCOUNTS` counts live guests only. — *Enforced by*: service ·
    *Locked by*: `tests/services/test_guest_service.py::test_cleanup_expired_guests_prunes_only_expired`,
    `::test_guest_cap_counts_live_guests_only`,
    `tests/api/test_auth_guest.py::test_multiple_guest_sessions_are_isolated`,
    `::test_demo_session_refused_when_guest_sandbox_disabled`, `::test_guest_sandbox_runs_over_https`,
    `::test_guest_upload_is_refused`, `::test_guest_can_import_a_sample_statement`
16. **Auth endpoints are rate-limited** per client key. With `RATE_LIMIT_TRUST_PROXY` the key is the
    rightmost `X-Forwarded-For` entry (one trusted hop); enable it only when the API is reachable solely
    through that proxy. — *Enforced by*: service ·
    *Locked by*: `tests/test_rate_limit.py::test_trust_proxy_takes_rightmost_entry_on_multi_hop`
17. **Logs never carry PAN, card numbers, account numbers or 12-digit Aadhaar numbers**; masking runs at
    the logger. A card is stored as its last 4 digits only, never alongside its first 6. —
    *Enforced by*: logger processor ·
    *Locked by*: `tests/api/test_http_logging.py::test_pii_processor_scrubs_aadhaar_in_exception_string`

## Out of bounds

- No Postgres row-level security: it is not portable to SQLite (ADR-0001).
- No household grouping in v1. Joint accounts are imported by each user separately; rows are never
  shared, so nothing double-counts. Design nothing that blocks an additive household view later.
- No instant cross-device revocation: "sign out other devices" lags by up to the access TTL
  (locked by `test_access_token_survives_password_change_until_expiry`).
- No email verification and no non-enumerating register response: both need email delivery.

## Why

Real data lives only on a self-hosted instance (one user or a [household](../../GLOSSARY.md#users--tenancy)),
which may be WAN-exposed and hold mutually untrusted users. httpOnly cookies keep the session out of
JavaScript; rotation with reuse detection makes a stolen refresh token self-limiting. OWASP requires
both an idle and an absolute timeout. The shared demo account's password is in the source and, on a
dev-seeded install, it owns real data — so it stays off HTTPS. A hosted demo serves only guests, and
since the DPDP household exemption does not cover the project's own demo, guests see sample
statements, never real ones. A user can erase their own data at any time (the DPDP Act's right to
erasure, s.12(3)); erasure reuses guest teardown, so one tested path deletes every owned table.
Composite FKs make isolation a DB fact rather than a code-review hope.

## Alternatives rejected

- **Tokens in the body or localStorage** — readable by JavaScript, so XSS exfiltrates the session.
- **Stateless JWT only** — change-password and logout could not kill sessions.
- **Plain FKs plus service checks** — one forgotten check (restore, bulk endpoint) stores a leak.
- **Gate the demo on reachability** — under the proxy stack a LAN-reachable box reads as local.
- **One gate for demo login and guests** — cannot offer hosted guests without exposing the shared login.
- **Deletion with a grace period** — needs a scheduler and a pending-deletion state that no other
  feature uses; the password, confirmation phrase and backup offer guard against accidents instead.
- **Registration open by default** — any WAN instance becomes an enumeration oracle for member emails.

## Consequences

- The isolation matrix grows with every owned table; that is the price of the guarantee.
- **Reopen at a multi-worker or Postgres deploy**: per-worker rate-limit buckets need a shared store;
  revoke-then-insert on change-password races under `READ COMMITTED`; instant revocation needs a
  session-epoch check.

## History

- 2026-07-18 — Accepted: cookie JWT, rotating refresh, per-row `user_id`, global reference tables.
- 2026-08-02 — Demo login also requires `DEMO_LOGIN_ENABLED`; `cookie_secure` alone was unassertable
  on the LAN proxy stack.
- 2026-08-08 — Open self-service signup brought into scope; the 409's LAN-only acceptance lapsed.
- 2026-11-15 — Rewritten to the rules-first template. Composite same-user FKs everywhere; read
  exemptions closed to identity lookups; central matrix plus teardown test; first-user bootstrap then
  registration closed by default; 401 for a deleted user; hosted guest sandboxes adopted behind their
  own gate with sample statements only, while the shared demo login stays off HTTPS; 30-minute idle
  timeout; Aadhaar masking; joint-account note.
- 2026-11-15 — Added rule 13b: immediate self-service account deletion reusing guest teardown.
- 2026-11-15 — Rule 13b: the confirmation phrase is named; a wrong password is 400 (a body error on a
  valid session); teardown lives in services/user_teardown.py, shared with guest expiry.
- 2026-11-16 — Rule 13b: the client renders from the server-computed
  `UserRead.can_manage_credentials`, never from the email or a guest flag.
