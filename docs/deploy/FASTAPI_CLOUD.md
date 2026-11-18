# Deploying Demo-Only Full-Stack Instance to FastAPI Cloud

This guide provides instructions for deploying the **fin-tracker full-stack application (FastAPI backend + Next.js static frontend)** to **FastAPI Cloud** in a **multi-user interactive demo mode** using SQLite with WAL concurrency and ephemeral sandboxes.

---

## 1. Overview

In this deployment configuration:
- **Unified Full-Stack Container**: FastAPI Cloud hosts both the `/api/v1` backend and the Next.js React frontend on the same domain via FastAPI's `app.frontend()`.
- **Ephemeral Multi-Visitor Sandboxes (`POST /api/v1/auth/demo-session`)**: Each visitor clicking **"Try the demo"** is provisioned their own isolated guest tenant with sample accounts, transactions, and categories in **< 25ms** (omitting Argon2 hashing for guest sessions). Multiple visitors can test adding, editing, or deleting transactions simultaneously with zero cross-tenant contamination.
- **SQLite Concurrency Hardening**: Configured with `PRAGMA journal_mode=WAL;`, `PRAGMA busy_timeout=5000;`, and `PRAGMA synchronous=NORMAL;` for non-blocking concurrent reads and write queueing.
- **Automatic TTL Pruning**: A background async sweeper runs every 15 minutes in the FastAPI application lifespan to cleanly delete expired guest sandboxes (2-hour TTL) using a deterministic 6-phase topological teardown.
- **Tag-Driven & On-Demand Deployments**: GitHub Actions deploys the container on semantic version release tags (`v*.*.*`) or on-demand via `workflow_dispatch`. (Guest sandboxes anchor rolling 90-day transaction windows on-the-fly via `clock.today()` on every session creation, eliminating the need for daily cron redeploys).
- **User Registration is Closed** (`REGISTRATION_ENABLED=false`, the default) once the first user exists, preventing unauthorized permanent user accounts on the public showcase. The first user can always register, so **register the operator account right after the first deploy** (see §4 step 4): on a guest-only deploy any visitor could otherwise claim that first account.
- **Zero CORS / Cookie Friction**: Because the UI and API reside on the same origin, auth cookies work natively without cross-origin configuration.

---

## 2. Prerequisites

1. Install the FastAPI CLI tool:
   ```bash
   # Using uv (recommended)
   uv tool install "fastapi[standard]"

   # Or using pip
   pip install "fastapi[standard]"
   ```

   > **Windows Tip**: Set UTF-8 encoding in PowerShell if your terminal shows Unicode/emoji encoding warnings:
   > ```powershell
   > $env:PYTHONIOENCODING="utf-8"
   > ```

2. Authenticate with FastAPI Cloud:
   ```bash
   fastapi login
   ```

---

## 3. Environment Variables Configuration

Configure environment variables via the [FastAPI Cloud Dashboard](https://dashboard.fastapicloud.com) or using the CLI:

```bash
# Non-secret configuration
fastapi cloud env set ENVIRONMENT "production"
fastapi cloud env set COOKIE_SECURE "true"
fastapi cloud env set COOKIE_SAMESITE "none"
fastapi cloud env set GUEST_SANDBOX_ENABLED "true"
fastapi cloud env set REGISTRATION_ENABLED "false"
fastapi cloud env set SEED_DEMO_ON_STARTUP "true"
fastapi cloud env set APPLY_MIGRATIONS_ON_STARTUP "true"
fastapi cloud env set RATE_LIMIT_TRUST_PROXY "true"

# Secrets (encrypted in FastAPI Cloud)
fastapi cloud env set --secret JWT_SECRET "<generate-random-64-char-string>"
```

| Variable | Recommended Value | Purpose |
|---|---|---|
| `ENVIRONMENT` | `production` | Enables production logger and optimizations |
| `JWT_SECRET` | `<generate-random-64-char-string>` | Secret key for signing session tokens (set with `--secret`) |
| `COOKIE_SECURE` | `true` | Enforces `Secure` flag on session cookies (required over HTTPS) |
| `COOKIE_SAMESITE` | `none` | Allows auth cookies over HTTPS |
| `GUEST_SANDBOX_ENABLED` | `true` | Enables guest sandboxes at `POST /auth/demo-session` (allowed over HTTPS). Leave `DEMO_LOGIN_ENABLED` unset: the shared demo login never runs over HTTPS |
| `REGISTRATION_ENABLED` | `false` | Closes `/auth/register` and hides registration links once the first user has registered (the default; set explicitly for clarity) |
| `SEED_DEMO_ON_STARTUP` | `true` | Auto-populates 90-day demo transactions and investments on boot |
| `APPLY_MIGRATIONS_ON_STARTUP` | `true` | Automatically brings SQLite schema to `head` on boot |
| `RATE_LIMIT_TRUST_PROXY` | `true` | Keys rate-limiter on client IP via X-Forwarded-For behind cloud ingress |

---

## 4. Building & Deploying the Application

### 1. Build the Static Frontend
Compile the Next.js frontend with relative API path routing:

```bash
# In frontend/
cd frontend
NEXT_PUBLIC_API_BASE_URL="/api/v1" pnpm build
```

### 2. Stage Static Assets into Backend
Copy the generated `frontend/out/` directory into `backend/frontend_dist/`:

```bash
# From project root
mkdir -p backend/frontend_dist
cp -r frontend/out/* backend/frontend_dist/
```

### 3. Deploy to FastAPI Cloud
Navigate to the `backend/` directory and run:

```bash
cd backend
fastapi deploy
```

FastAPI Cloud will package the backend and pre-built frontend bundle, build the container, provision HTTPS (`https://fin-tracker-demo.fastapicloud.dev`), and launch the application.

### 4. Register the Operator Account Immediately
The first user can always register, whatever `REGISTRATION_ENABLED` says (ADR-0003 rule 12). Right after the first deploy, register your own account at `/register`; registration closes as soon as it exists. Until then any visitor to a guest-only deploy could claim that first account.

---

## 5. Automated CI/CD (GitHub Actions)

The repository includes a dedicated workflow [`.github/workflows/deploy.yml`](../../.github/workflows/deploy.yml) that builds the static frontend, executes backend quality gates (`ruff`, `ty`), and deploys to FastAPI Cloud.

### Setting Up GitHub Secrets via GitHub CLI (`gh`)

Generate a dedicated CI deploy token (or copy it from the FastAPI Cloud dashboard) and set the secrets using `--body` to prevent CRLF newline corruption:

```powershell
# Create a dedicated deploy token via CLI
fastapi cloud tokens create --name "github-actions-ci"

# Set GitHub Secrets with clean body input
gh secret set FASTAPI_CLOUD_TOKEN --body "<your-deploy-token>" --repo krashu/fin-tracker
gh secret set FASTAPI_CLOUD_APP_ID --body "8a2313e9-ac17-4761-add4-20a4b23a0655" --repo krashu/fin-tracker
```

### How the Workflow Operates

- **Release Tag Triggers**: Deploys on git release tags matching `v*.*.*` (e.g. `git tag v0.1.0 && git push origin v0.1.0`).
- **Manual Trigger**: Can be dispatched on demand via GitHub Actions UI or `gh workflow run deploy.yml`.
- **Quality Gates**: Runs full docker validation, backend test suite, frontend test suite, security redaction checks, and pre-deploy lint/typecheck before initiating deployment.
- **Automated Frontend Build**: Compiles Next.js with `NEXT_PUBLIC_API_BASE_URL="/api/v1"` and stages static assets into `backend/frontend_dist/`.
- **Post-Deploy Health Check**: Automatically probes `GET /api/v1/auth/config` to verify the deployment booted successfully.

---

## 6. Architecture & Concurrency Guarantees

### 1. Ephemeral Guest Sandboxes (`guest_service.py`)
- **Fast Sandbox Provisioning**: Bypasses Argon2 password hashing (`password_hash=None`) for guests, dropping sandbox creation latency to < 25ms and eliminating CPU-exhaustion denial of service risks.
- **Deterministic 6-Phase Teardown** (`user_teardown.teardown_user`, shared with account deletion):
  1. Nullify cyclic FKs (`transfer_pair_id`, `investment_transactions.pair_id`, `parent_account_id`).
  2. Delete join/mapping rows (`transaction_labels`, `merchant_label_maps`, `merchant_tag_maps`, `merchant_aliases`).
  3. Delete transactions (`transactions`, `investment_transactions`, `import_batches`).
  4. Delete entities (`labels`, `instruments`, `accounts`).
  5. Delete categories (subcategories with `parent_id IS NOT NULL` first, root categories second).
  6. Delete sessions and the user row.
- **Safety Invariant** (in `delete_guest_sandbox`, not the shared teardown): Refuses to touch registered / permanent user accounts (`is_guest=False`).

### 2. SQLite WAL & Busy Timeout
- Connect listeners automatically execute `PRAGMA journal_mode=WAL;`, `PRAGMA busy_timeout=5000;`, and `PRAGMA synchronous=NORMAL;` so reader queries never block writers, and concurrent writes queue gracefully up to 5,000ms.

### 3. Origin CSRF Protection (`OriginCSRFMiddleware`)
- All state-mutating requests (`POST`, `PUT`, `PATCH`, `DELETE`) require a valid `Origin` header matching the deployment host, preventing cross-site request forgery attacks.

---

## 7. Verification Checklist

1. **Auth Config Endpoint**:
   ```bash
   curl https://fin-tracker-demo.fastapicloud.dev/api/v1/auth/config
   ```
   *Expected Response (after the operator account exists):* `{"demo_login_enabled": false, "guest_sandbox_enabled": true, "registration_enabled": false}`

2. **Ephemeral Guest Session Creation**:
   ```bash
   curl -i -X POST https://fin-tracker-demo.fastapicloud.dev/api/v1/auth/demo-session \
     -H "Origin: https://fin-tracker-demo.fastapicloud.dev"
   ```
   *Expected Response:* `201 Created` with `is_guest: true` and `Set-Cookie` headers for `access_token` and `refresh_token`.

3. **Registration Endpoint Rejected**:
   ```bash
   curl -X POST https://fin-tracker-demo.fastapicloud.dev/api/v1/auth/register \
     -H "Content-Type: application/json" \
     -d '{"email": "test@example.com", "password": "password123"}'
   ```
   *Expected Response (after the operator account exists):* `403 Forbidden` (`{"detail": "registration is disabled"}`)
