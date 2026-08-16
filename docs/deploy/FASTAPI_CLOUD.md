# Deploying Demo-Only Full-Stack Instance to FastAPI Cloud

This guide provides instructions for deploying the **fin-tracker full-stack application (FastAPI backend + Next.js static frontend)** to **FastAPI Cloud** in a **multi-user interactive demo mode** using SQLite with WAL concurrency and ephemeral sandboxes.

---

## 1. Overview

In this deployment configuration:
- **Unified Full-Stack Container**: FastAPI Cloud hosts both the `/api/v1` backend and the Next.js React frontend on the same domain via FastAPI's `app.frontend()`.
- **Ephemeral Multi-Visitor Sandboxes (`POST /api/v1/auth/demo-session`)**: Each visitor clicking **"Try the demo"** is provisioned their own isolated guest tenant with sample accounts, transactions, and categories in **< 25ms** (omitting Argon2 hashing for guest sessions). Multiple visitors can test adding, editing, or deleting transactions simultaneously with zero cross-tenant contamination.
- **SQLite Concurrency Hardening**: Configured with `PRAGMA journal_mode=WAL;`, `PRAGMA busy_timeout=5000;`, and `PRAGMA synchronous=NORMAL;` for non-blocking concurrent reads and write queueing.
- **Automatic TTL Pruning**: A background async sweeper runs every 15 minutes in the FastAPI application lifespan to cleanly delete expired guest sandboxes (2-hour TTL) using a deterministic 6-phase topological teardown.
- **Daily Automated Deployments**: GitHub Actions automatically rebuilds and redeploys the container daily at 03:17 UTC, anchoring rolling 90-day transaction windows to `clock.today()`.
- **User Registration is Disabled** (`REGISTRATION_ENABLED=false`), preventing unauthorized permanent user accounts on the public showcase.
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
fastapi cloud env set DEMO_LOGIN_ENABLED "true"
fastapi cloud env set ALLOW_DEMO_LOGIN_OVER_HTTPS "true"
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
| `DEMO_LOGIN_ENABLED` | `true` | Enables authentication with demo credentials and guest sessions |
| `ALLOW_DEMO_LOGIN_OVER_HTTPS` | `true` | Authorizes demo authentication on HTTPS deployments |
| `REGISTRATION_ENABLED` | `false` | Disables `/auth/register` and hides registration links |
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

---

## 5. Automated CI/CD (GitHub Actions)

The repository includes a dedicated workflow [`.github/workflows/deploy.yml`](../../.github/workflows/deploy.yml) that builds the static frontend, executes backend quality gates (`ruff`, `ty`), and deploys to FastAPI Cloud.

### Setting Up GitHub Secrets via GitHub CLI (`gh`)

You can set the required repository secrets directly using the GitHub CLI:

```powershell
# Extract your active token from local CLI config
$token = (Get-Content "$env:APPDATA\fastapi-cli\auth.json" | ConvertFrom-Json).access_token

# Set GitHub Secrets
$token | gh secret set FASTAPI_CLOUD_TOKEN --repo krashu/fin-tracker
"8a2313e9-ac17-4761-add4-20a4b23a0655" | gh secret set FASTAPI_CLOUD_APP_ID --repo krashu/fin-tracker
```

### How the Workflow Operates

- **Push Triggers**: Runs on pushes to `main` modifying `backend/**`, `frontend/**`, or `.github/workflows/deploy.yml`.
- **Scheduled Cron**: Runs daily (`cron: "17 3 * * *"`) at 03:17 UTC to ensure rolling demo dates stay anchored to current dates.
- **Manual Trigger**: Can be dispatched on demand via `gh workflow run deploy.yml`.
- **Automated Frontend Build**: Compiles Next.js with `NEXT_PUBLIC_API_BASE_URL="/api/v1"` and stages assets into `backend/frontend_dist/`.
- **Pre-Deploy Quality Gates**: Runs `ruff check .` and `ty check app` before initiating `fastapi deploy`.

---

## 6. Architecture & Concurrency Guarantees

### 1. Ephemeral Guest Sandboxes (`guest_service.py`)
- **Fast Sandbox Provisioning**: Bypasses Argon2 password hashing (`password_hash=None`) for guests, dropping sandbox creation latency to < 25ms and eliminating CPU-exhaustion denial of service risks.
- **Deterministic 6-Phase Teardown**:
  1. Nullify cyclic FKs (`transfer_pair_id`, `investment_transactions.pair_id`, `parent_account_id`).
  2. Delete join/mapping rows (`transaction_labels`, `merchant_label_maps`, `merchant_tag_maps`, `merchant_aliases`).
  3. Delete transactions (`transactions`, `investment_transactions`, `import_batches`).
  4. Delete entities (`labels`, `instruments`, `accounts`).
  5. Delete categories (subcategories with `parent_id IS NOT NULL` first, root categories second).
  6. Delete sessions and guest user row.
- **Safety Invariant**: Refuses to touch registered / permanent user accounts (`is_guest=False`).

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
   *Expected Response:* `{"demo_login_enabled": true, "registration_enabled": false}`

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
   *Expected Response:* `403 Forbidden` (`{"detail": "registration is disabled"}`)
