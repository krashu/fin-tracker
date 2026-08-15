# Fin Tracker

A privacy-first, self-hosted personal finance tracker for Indian banks and global investments — import statements, auto-tag transactions, manage hierarchical categories, track an INR+USD portfolio with proper XIRR, and analyze spending trends locally.

![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.13-blue)
![Node](https://img.shields.io/badge/node-24-brightgreen)
![Next.js](https://img.shields.io/badge/Next.js-16_App_Router-black)
![Tailwind](https://img.shields.io/badge/Tailwind_CSS-4.0-38bdf8)
![Tests](https://img.shields.io/badge/tests-pytest_%7C_vitest-success)

> Self-hosted and privacy-first — your statements, transactions, and portfolio data never leave your machine.

## Screenshots

> _Coming soon._

## Key Features

- 📥 **Statement Import & Staging**: Credit card & bank statement import (CSV + password-protected PDFs for ICICI / Axis) with structured `ParsedStatement` returns, balance reconciliation, and an interactive review queue (`/imports/review/[batchId]`).
- 🏷️ **Hierarchical 2-Level Categorization**: Full two-level category tree (ADR-0012) with one-hop rollup, color inheritance, soft archive cascades, and a command-palette (`cmdk`) integrated category selector.
- 🤖 **Smart Auto-Tagging & Merchant Aliases**: Automatically learns merchant→category mappings and canonical merchant aliases across recurring transactions.
- ✍️ **Expenses Board & Manual Entry**: Fast transaction management with date-aware formatting, multi-selection batch edits, custom transaction labels, and instant privacy balance masking (`Sensitive` over-the-shoulder masking).
- 📊 **Spending Analytics & Visualizations**:
  - Spend-by-category overview and monthly breakdown.
  - Interactive hierarchical donut charts (`HierarchicalDonutChart`) and trend bars (`HierarchicalTrendBar`, `CategoryTrendBar`).
  - Subcategory mover analysis (`SubcategoryMovers`) highlighting top spending shifts.
  - Summary metrics strip with income/spend/savings tracking.
- 📈 **Investments & Portfolio Tracking**:
  - Canonical transaction-CSV import for MFs & equities from any broker/AMC export.
  - US stocks/ETFs via INDmoney transaction export with per-transaction FX stamping.
  - INR home currency with USD investment rollup.
  - Scalar XIRR alpha against Indian benchmark index funds.
- 🧾 **Tax & Capital Gains Reporting**: Capital-gains statement (FIFO STCG/LTCG split, debt-slab flag, ₹1.25L exemption) + dividend-income summary.
- 💾 **Backup, Restore & Migration**: Comprehensive JSON/CSV export and import service with transaction deduplication and schema migration compatibility.
- 🔒 **Multi-User Tenant Isolation**: Secure cookie JWT authentication with rotating refresh tokens and strict per-row tenant isolation (`user_id`).

Full scope in [PRD.md](PRD.md).

## Architecture

```
                           ┌────────────────────────────────────────────────────────┐
                           │   Browser (Next.js 16 App Router + TanStack Query v5)  │
                           └───────────────────────────┬────────────────────────────┘
                                                       │ HTTP / JSON (Cookie JWT)
                                                       ▼
┌───────────────────────────┐   ┌───────────────────────────────────────────────────┐
│   PDF / CSV statements    │──▶│      FastAPI Backend (Python 3.13)                │
│ (ICICI, Axis, Broker CSV) │   │  • Parsers (pdfplumber + pikepdf)                 │
└───────────────────────────┘   │  • Services (dedup, auto-tagging, XIRR, backup)   │
                                │  • SQLAlchemy 2.0 (naive UTC clock, tenant-bound) │
                                └─────────────────────────┬─────────────────────────┘
                                                          │
                                                          ▼
                                ┌───────────────────────────────────────────────────┐
                                │             SQLite Database / Postgres            │
                                └───────────────────────────────────────────────────┘
```

The frontend uses Next.js 16 App Router with React 19 and TanStack Query v5 for client state management. Pydantic schemas are hand-mirrored as TypeScript types in `frontend/lib/api/client.ts`. Live tiles use TanStack Query invalidation contracts (`lib/queries/invalidate.ts`) to keep views synchronized.

## Tech stack

| Layer              | Choice                                                                        |
| ------------------ | ----------------------------------------------------------------------------- |
| **Backend**        | Python 3.13 + FastAPI + SQLAlchemy 2.0 + Alembic                              |
| **Database**       | SQLite (with Postgres portability via ADR-0001)                               |
| **PDF Parsing**    | `pdfplumber` + `pikepdf` (per-issuer strategy pattern)                        |
| **Financial Math** | `pyxirr`                                                                      |
| **Frontend**       | Next.js 16 App Router + React 19 + TypeScript + Tailwind CSS 4                |
| **UI Components**  | shadcn/ui (Radix primitives) + Lucide Icons + Recharts                        |
| **State & Cache**  | TanStack Query v5 (manual invalidation architecture)                          |
| **Testing**        | `pytest` + `pytest-xdist` (backend, 75% coverage gate), `vitest` (frontend)    |
| **Tooling & CI**   | `uv` (Python), `pnpm` (Node), `ruff`, Astral `ty`, `eslint`, GitHub Actions   |

Full rationale in [PRD.md § Tech stack](PRD.md#tech-stack).

## Deploy with Docker

Run the whole app on your own computer with Docker — your financial data never leaves your machine.

### 1. Install Docker

- **Windows / macOS** — install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and start it.
- **Linux** — install [Docker Engine](https://docs.docker.com/engine/install/) + Compose plugin (v2.24+).

### 2. Clone and Start

```bash
git clone <repository-url>
cd fin-tracker
docker compose -f docker-compose.yml up -d --build
```

### 3. Open the App

Open **http://localhost:3000** in your browser and register your account.

### Managing Docker Containers

| Task            | Command                                                                            |
| --------------- | ---------------------------------------------------------------------------------- |
| **Stop**        | `docker compose -f docker-compose.yml down`                                        |
| **Start again** | `docker compose -f docker-compose.yml up -d`                                       |
| **View logs**   | `docker compose -f docker-compose.yml logs -f`                                     |
| **Update**      | `git pull && docker compose -f docker-compose.yml up -d --build`                   |
| **Back up**     | Stop the container and copy the **`data/`** directory.                             |

### Reverse Proxy & LAN Access

To access the app across your local network or phone:
```bash
docker compose -f docker-compose.yml -f docker-compose.proxy.yml up -d --build
```
Access via **http://localhost:8080** (or `http://<your-host-ip>:8080`). Ensure your host IP is listed in `CORS_ALLOWED_ORIGINS` in `.env`.

---

## Local Development Setup

### Prerequisites
- **Python 3.13** + **uv** ([astral.sh/uv](https://docs.astral.sh/uv/))
- **Node 24** + **pnpm 11** (`npm install -g pnpm@11.21.0`)
- *(Optional)* **make**

### 1. Backend Setup

```bash
cd backend
uv sync
uv run alembic upgrade head   # Run database migrations
uv run uvicorn app.main:app --reload
```
API runs at **http://localhost:8000** (interactive Swagger docs at **http://localhost:8000/docs**).

### 2. Frontend Setup

```bash
cd frontend
pnpm install
pnpm dev
```
Frontend runs at **http://localhost:3000**.

### 3. Running with Make (Alternative)

```bash
make dev        # Hot-reload full stack via docker compose
make migrate    # Run alembic migrations
make backend    # Run backend locally with uvicorn reload
make frontend   # Run frontend dev server
```

---

## Testing & Quality Gates

The project maintains high code quality and strict type safety enforced through local hooks and GitHub Actions CI:

| Task                     | Command                                                              |
| ------------------------ | -------------------------------------------------------------------- |
| **Backend Tests**        | `cd backend && uv run pytest -n auto` (enforces 75% coverage floor)   |
| **Backend Fast Loop**    | `cd backend && uv run pytest --no-cov -m "not real_pdf and not slow"`|
| **Frontend Tests**       | `cd frontend && pnpm test` (Vitest unit tests)                       |
| **Backend Lint**         | `cd backend && uv run ruff check .`                                  |
| **Backend Typecheck**    | `cd backend && uv run python -m ty check app`                        |
| **Frontend Lint**        | `cd frontend && pnpm lint`                                           |
| **Frontend Typecheck**   | `cd frontend && pnpm typecheck` (`tsc --noEmit`)                      |
| **Pre-commit Checks**    | `cd backend && uv run pre-commit run --all-files`                     |

### CI/CD Workflows (`.github/workflows/`)

- **`test-backend.yml`**: Runs pytest suite across python matrix, enforces 75% coverage and migration stairway verification.
- **`test-frontend.yml`**: Runs `vitest`, `tsc --noEmit`, and ESLint across frontend components and utilities.
- **`docker-build.yml`**: Validates Docker container builds for both backend and frontend.
- **`security.yml`**: CodeQL static analysis and dependency vulnerability scans.
- **`pre-commit.yml`**: Validates pre-commit hook compliance including fixture redaction checks.
- **Dependabot**: Automated dependency tracking and security patches.

---

## Configuration

Environment variables live in `.env` at the project root (copy from [.env.example](.env.example)):

| Variable         | Default                             | Purpose                                                          |
| ---------------- | ----------------------------------- | ---------------------------------------------------------------- |
| `API_HOST`       | `127.0.0.1`                         | Backend bind address (`0.0.0.0` for LAN access).                 |
| `API_PORT`       | `8000`                              | Backend API port.                                                |
| `API_RELOAD`     | `false`                             | Enable uvicorn hot reloading in development.                     |
| `LOG_FORMAT`     | `console`                           | Log output format (`console` or `json` for production).          |
| `LOG_LEVEL`      | `info`                              | App log level (`debug`, `info`, `warning`, `error`).             |
| `DATABASE_URL`   | `sqlite:///./data/fin-tracker.db`   | Database connection string (SQLite or Postgres).                 |
| `JWT_SECRET`     | seeded dev secret                   | Secret key used for signing JWT session tokens.                  |
| `V1_USER_ID`     | seeded UUID                         | Default user ID matching Alembic seeded user row.                |

---

## Troubleshooting

- **`uv sync` fails with TLS / certificate errors**: Force the public PyPI index: `UV_INDEX= uv sync`.
- **VS Code shows missing Python imports**: Press `Ctrl+Shift+P` → "Python: Select Interpreter" → choose `backend/.venv/Scripts/python.exe`.
- **`pnpm: command not found` on Windows**: Ensure `%APPDATA%\npm` is added to your user PATH: `setx PATH "%PATH%;%APPDATA%\npm"`.
- **`pnpm install` postinstall script errors**: `frontend/pnpm-workspace.yaml` is configured to build `sharp` and `unrs-resolver`. If shims break on Windows, run `corepack pnpm install --force`.
- **Corporate TLS Proxy / Zscaler**: See [frontend/certs/README.md](frontend/certs/README.md) for the `ca-bundle` automatic merge workflow.

---

## Contributing

Contributions, issues, and feature requests are welcome! Feel free to open an issue or submit a pull request.

Before submitting a pull request, ensure all test suites and linting checks pass:

```bash
# Backend validation
cd backend && uv run pytest -n auto && uv run ruff check . && uv run python -m ty check app

# Frontend validation
cd frontend && pnpm test && pnpm typecheck && pnpm lint
```

---

## License

Distributed under the MIT License. See [LICENSE](LICENSE) for more details.
