# Deploying Demo-Only Instance to FastAPI Cloud

This guide provides instructions for deploying the **fin-tracker backend** to **FastAPI Cloud** in a **demo-only showcase mode** using SQLite.

---

## 1. Overview

In this configuration:
- **FastAPI Cloud** hosts the FastAPI backend application with automatic HTTPS, autoscaling, and metrics.
- **SQLite** is used as the database engine.
- **Demo Dataset** is automatically seeded on startup with a rolling 90-day window ending at today (`SEED_DEMO_ON_STARTUP=true`).
- **User Registration is Disabled** (`REGISTRATION_ENABLED=false`), preventing public account creation.
- **Demo Login over HTTPS is Allowed** (`DEMO_LOGIN_ENABLED=true` + `ALLOW_DEMO_LOGIN_OVER_HTTPS=true`).
- **Frontend** displays a streamlined 1-click **"Try the demo"** experience and removes public registration forms.

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

## 4. Deploying the Backend

Because `backend/pyproject.toml` is configured with `[tool.fastapi]` entrypoint (`app.main:app`) and `backend/.fastapicloudignore` is in place, simply deploy with:

```bash
cd backend
fastapi deploy
```

FastAPI Cloud will package your files, build the container, provision HTTPS (`https://<project-name>.fastapicloud.dev`), and launch the application.

---

## 5. Connecting the Frontend

1. Deploy the `frontend/` to **Vercel**, **Cloudflare Pages**, or **Netlify**.
2. Set the environment variable on the frontend platform:
   ```env
   NEXT_PUBLIC_API_URL=https://<project-name>.fastapicloud.dev/api/v1
   ```
3. Update `CORS_ALLOWED_ORIGINS` in your FastAPI Cloud dashboard to match the production frontend URL.

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
