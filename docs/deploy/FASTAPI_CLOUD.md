# Deploying Demo-Only Full-Stack Instance to FastAPI Cloud

This guide provides instructions for deploying the **fin-tracker full-stack application (FastAPI backend + Next.js static frontend)** to **FastAPI Cloud** in a **demo-only showcase mode** using SQLite.

---

## 1. Overview

In this configuration:
- **FastAPI Cloud** hosts the unified full-stack application (serving both `/api/v1` and the React frontend on the same domain via `app.frontend()`).
- **SQLite** is used as the database engine.
- **Demo Dataset** is automatically seeded on startup with a rolling 90-day window ending at today (`SEED_DEMO_ON_STARTUP=true`).
- **User Registration is Disabled** (`REGISTRATION_ENABLED=false`), preventing public account creation.
- **Demo Login over HTTPS is Allowed** (`DEMO_LOGIN_ENABLED=true` + `ALLOW_DEMO_LOGIN_OVER_HTTPS=true`).
- **Frontend** provides an instant 1-click **"Try the demo"** exploration experience.
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
fastapi cloud env set CORS_ALLOWED_ORIGINS "https://your-frontend-app.vercel.app"
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
| `COOKIE_SAMESITE` | `none` | Allows cross-origin auth cookies between your frontend domain and FastAPI Cloud |
| `CORS_ALLOWED_ORIGINS` | `https://your-frontend-app.vercel.app` | Comma-separated list of allowed frontend origins |
| `DEMO_LOGIN_ENABLED` | `true` | Enables authentication with demo credentials |
| `ALLOW_DEMO_LOGIN_OVER_HTTPS` | `true` | Authorizes demo authentication on HTTPS deployments |
| `REGISTRATION_ENABLED` | `false` | Disables `/auth/register` and hides registration links |
| `SEED_DEMO_ON_STARTUP` | `true` | Auto-populates 90-day demo transactions and investments on boot |
| `APPLY_MIGRATIONS_ON_STARTUP` | `true` | Automatically brings SQLite schema to `head` on boot |
| `RATE_LIMIT_TRUST_PROXY` | `true` | Keys rate-limiter on client IP via X-Forwarded-For behind cloud ingress |

---

## 4. Building & Deploying the Full-Stack Application

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

FastAPI Cloud will package your backend and static frontend bundle, build the container, provision HTTPS (`https://<project-name>.fastapicloud.dev`), and launch the application.

---

## 5. Deployment Options

- **Unified Hosting (Recommended)**: Both frontend and API are served from `https://<project-name>.fastapicloud.dev` via FastAPI's `app.frontend()`.
- **Decoupled Hosting (Optional)**: If you prefer hosting the frontend on **Vercel** or **Cloudflare Pages**, set `NEXT_PUBLIC_API_BASE_URL=https://<project-name>.fastapicloud.dev/api/v1` on your hosting provider and set `CORS_ALLOWED_ORIGINS` on FastAPI Cloud.

---

## 6. Verification Checklist

1. **Auth Config Endpoint**:
   ```bash
   curl https://<project-name>.fastapicloud.dev/api/v1/auth/config
   ```
   *Expected Response:*
   ```json
   {
     "demo_login_enabled": true,
     "registration_enabled": false
   }
   ```
2. **Registration Endpoint Rejected**:
   ```bash
   curl -X POST https://<project-name>.fastapicloud.dev/api/v1/auth/register \
     -H "Content-Type: application/json" \
     -d '{"email": "test@example.com", "password": "password123"}'
   ```
   *Expected Response:* `403 Forbidden` (`{"detail": "registration is disabled"}`)
3. **Frontend Experience**:
   - Navigate to the frontend login URL.
   - Confirm the page displays the **"Try the demo"** button as the primary call-to-action without any signup link in the footer.
   - Click **"Try the demo"** to verify instantaneous sign-in to the demo dashboard.

---

## 7. Automated CI/CD (GitHub Actions)

The repository includes a dedicated workflow [`.github/workflows/deploy.yml`](../../.github/workflows/deploy.yml) that automatically runs code quality checks and deploys the backend to FastAPI Cloud whenever changes are pushed to `main`.

### Setting Up GitHub Secrets

1. **Create a Deploy Token**:
   - Via CLI:
     ```bash
     fastapi cloud tokens create --name "GitHub Actions CI" --expires-in-days 365
     ```
   - Or via Web Dashboard:
     Navigate to your app > **Deploy Tokens** > **Create Token**.

2. **Add Secrets to GitHub**:
   In your GitHub repository, go to **Settings > Secrets and variables > Actions > New repository secret** and add:

   | Secret Name | Description / Value |
   |---|---|
   | `FASTAPI_CLOUD_TOKEN` | The deploy token generated in Step 1. |
   | `FASTAPI_CLOUD_APP_ID` | Your app UUID: `8a2313e9-ac17-4761-add4-20a4b23a0655` |

### How the Workflow Operates

- **Scoped Triggers**: Runs on pushes to `main` modifying `backend/**`, `frontend/**`, or `.github/workflows/deploy.yml`, or via manual trigger (`workflow_dispatch`).
- **Automated Frontend Build**: Compiles the Next.js frontend with `NEXT_PUBLIC_API_BASE_URL="/api/v1"` and stages static assets into `backend/frontend_dist/`.
- **Atomic Rollout**: Uses `concurrency.cancel-in-progress: false` to ensure in-flight builds and database migrations complete without interruption.
- **Pre-Deploy Quality Gate**: Runs `ruff check` and `ty check app` before initiating `fastapi deploy` to prevent deploying regressed code.

---

## 8. Full-Stack Architecture & Troubleshooting

### 1. Same-Origin CSRF Protection (`OriginCSRFMiddleware`)
When the frontend and backend are served together on the same origin (e.g. `https://fin-tracker-demo.fastapicloud.dev`):
- Non-safe HTTP methods (`POST`, `PUT`, `PATCH`, `DELETE`) require an `Origin` header.
- `OriginCSRFMiddleware` automatically allows requests whose `Origin` header matches the incoming request host/protocol (`same_origin`), as well as any external origins specified in `CORS_ALLOWED_ORIGINS`.

### 2. Trailing Slash Routing (`trailingSlash: true`)
- Next.js static HTML export emits directory indices (e.g. `login/index.html`, `dashboard/index.html`).
- FastAPI's `app.frontend()` serves directory indices with trailing slashes (`/login/`, `/dashboard/`).
- Routes in `route-guard.tsx` and `login/page.tsx` normalize trailing slashes so checks succeed regardless of slash variations.

### 3. Local Development vs. Production Deployment
- **Local Dev**: Run backend on `:8000` (`uv run uvicorn ...`) and frontend on `:3000` (`pnpm dev`). Because `backend/frontend_dist/` is git-ignored, FastAPI serves purely the API and leaves Next.js hot reload untouched.
- **FastAPI Cloud**: Single container hosts both the API and the pre-built React frontend at `https://fin-tracker-demo.fastapicloud.dev`.
