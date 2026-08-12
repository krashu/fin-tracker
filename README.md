# Fin Tracker

Personal finance tracker for Indian banks + global investments — import statements, auto-tag transactions, track an INR+USD portfolio with proper XIRR, all locally.

![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.13-blue)
![Node](https://img.shields.io/badge/node-24-brightgreen)

> Self-hosted and privacy-first — your statements and portfolio data never leave your machine.

## Why this exists

Existing trackers (Mint, Lunchmoney, YNAB, Kuvera) each fall short on at least one axis: poor support for Indian bank statement formats, no investment-side depth, or SaaS hosting your full financial data. This is a self-hosted alternative that imports CC + bank PDFs from ICICI / Axis, tracks investments via a canonical transaction-CSV import (any broker / AMC export), handles US stocks held via INDmoney in USD with FX-adjusted rollups, and computes XIRR / allocation properly. Built primarily as a personal tool — also a portfolio project.

## Screenshots

> _Coming soon._

## Features

- 📥 Import credit-card & bank statements (CSV + password-protected PDF) — ICICI / Axis
- 🤖 Auto-tag transactions by learning your past merchant→category mappings
- ✍️ Manual transactions (cash, transfers, imports the parser missed)
- 📊 Investments via canonical transaction-CSV import — MFs & equities from any broker/AMC export
- 🌍 US stocks/ETFs via INDmoney transaction export, with per-transaction FX stamping
- 💱 INR home currency with USD investment rollup
- 📈 Live portfolio tiles + monthly spend dashboards
- 🏆 Portfolio vs benchmark — "am I beating the market?" against an index fund, in XIRR alpha
- 🧾 Capital-gains statement (FIFO STCG/LTCG split, debt-slab flag, ₹1.25L exemption) + dividend-income summary — to reconcile against your AMC report & AIS at filing
- 🗂️ One-click CSV export to Google Drive

**Coming soon:** recurring-transaction detection, OCR for scanned bills, budgets, multi-user households, capital-loss set-off + carry-forward, tax-loss harvesting.

Full scope in [PRD.md](PRD.md).

## Architecture

```
                           ┌──────────────────────────────────────┐
                           │   Browser (React + TanStack Query)   │
                           └──────────────────┬───────────────────┘
                                              │ HTTP / JSON
                                              ▼
┌────────────────┐   ┌──────────────────────────────────────────┐
│   PDF / CSV    │──▶│  FastAPI  ─ parsers ─ services ─ models  │
│   statements   │   │       │                                  │
└────────────────┘   │       └──▶  SQLAlchemy ──▶  SQLite        │
                     │                                          │
                     │       └──▶  Google Drive (CSV backup)     │
                     └──────────────────────────────────────────┘
```

Server-rendered? No — Next.js is the React frontend, FastAPI is the JSON API. The four consumed Pydantic schemas are hand-mirrored as TypeScript types in `frontend/lib/api/client.ts`. Live tiles use TanStack Query polling today; a push-based SSE channel is planned.

## Tech stack

| Layer        | Choice                                                      |
| ------------ | ----------------------------------------------------------- |
| Backend      | Python 3.13 + FastAPI + SQLAlchemy 2 + Alembic              |
| DB           | SQLite, with Postgres support planned                       |
| PDF parsing  | pdfplumber + pikepdf (per-issuer strategy pattern)          |
| XIRR         | `pyxirr`                                                     |
| Frontend     | Next.js 16 LTS + React 19 + TypeScript 7 + Tailwind CSS 4   |
| UI kit       | shadcn/ui (Radix primitives) + Recharts via shadcn `Chart`  |
| Data layer   | TanStack Query v5                                           |
| Tooling      | uv (Python) / pnpm (Node) / pre-commit / ruff / ty / eslint |

Full rationale in [PRD.md § Tech stack](PRD.md#tech-stack).

## Deploy with Docker

Run the whole app on your own computer with Docker — your financial data never leaves your
machine. You don't need Python, Node, or any of the developer tools below; Docker builds
everything for you.

### 1. Install Docker

- **Windows / macOS** — install [Docker Desktop](https://www.docker.com/products/docker-desktop/)
  and start it (wait until the whale icon says it's running).
- **Linux** — install [Docker Engine](https://docs.docker.com/engine/install/) + the Compose
  plugin (v2.24 or newer).

Confirm it works: `docker --version`.

### 2. Get the code

```bash
git clone <repository-url>
cd fin-tracker
```

No git? On the project's page use **Code → Download ZIP**, unzip it, and open a terminal in that
folder.

### 3. Start it

```bash
docker compose -f docker-compose.yml up -d --build
```

The first run takes a few minutes while Docker builds the images. `-d` runs it in the
background, so you can close the terminal afterwards. *(If you have `make`, `make up` is a
shortcut — it just runs in the foreground.)*

### 4. Open it

On **this computer**, open **http://localhost:3000** and **create your account**. (A demo account
— `demo@fin-tracker.local` / `demofintracker` — also exists, but its password is published in
this repo, so **its login is refused unless you set `DEMO_LOGIN_ENABLED=true`** in `.env`. Leave
it off: this stack is reachable from your LAN, and that account is the row your own data would
hang off on an upgraded install. `make dev` enables it for throwaway dev boxes.)

On the very first run, give it a minute, then confirm both parts are up:

```bash
docker compose -f docker-compose.yml ps
```

`backend` and `frontend` should both show `running` (backend `healthy`). That's the whole thing
for personal use on your own machine.

### Managing it

| Task        | Command                                                                    |
| ----------- | -------------------------------------------------------------------------- |
| Stop        | `docker compose -f docker-compose.yml down`                                |
| Start again | `docker compose -f docker-compose.yml up -d`                               |
| See logs    | `docker compose -f docker-compose.yml logs -f`                             |
| Update      | Get the latest code (`git pull`, or re-download the ZIP), then re-run **Start it**. |
| **Back up** | Stop it, then copy the whole **`data/`** folder — that's your entire history. |

### If it doesn't start

- **Nothing loads / "cannot connect":** make sure Docker Desktop is actually running, then
  re-run **Start it** and watch `docker compose -f docker-compose.yml logs -f`.
- **"port is already allocated":** another program is using `3000` or `8000` — close it (or stop
  the other container), then start again.

### Options & notes

- **One address / use it from your phone:** run
  `docker compose -f docker-compose.yml -f docker-compose.proxy.yml up -d --build`
  (or `make up-proxy`) and open **http://localhost:8080**. To reach it from another device on
  your Wi-Fi, add **this computer's own address** (e.g. `http://192.168.1.42:8080`, not the
  phone's) to `CORS_ALLOWED_ORIGINS` — see the notes in
  [docker-compose.proxy.yml](docker-compose.proxy.yml).
- **Before exposing it beyond your own computer,** set a secret: copy [.env.example](.env.example)
  to `.env` and set `JWT_SECRET` to a long random value (e.g. `openssl rand -base64 48`). Safe
  to skip for local-only use.
- **Use a different database (advanced):** the default is a local SQLite file under `data/`. To
  point at your own database (e.g. Postgres), set `DATABASE_URL` in `.env` — it overrides the
  default. (A Docker SQLite path needs four slashes: `sqlite:////data/fin-tracker.db`.)
- **Linux hosts only:** the backend runs as a non-root user (uid 10001). Make the data folder
  writable by it once, before step 3:
  ```bash
  mkdir -p data && sudo chown -R 10001:10001 data
  ```
  Not needed on Docker Desktop (Windows/macOS).

## Quickstart

### Prerequisites
- **Python 3.13** + **uv** (`pip install uv` or [astral.sh/uv](https://docs.astral.sh/uv/))
- **Node 24** + **pnpm 11**
- *(Optional)* **make** — Linux/macOS native; Windows via `choco install make` or `scoop install make`

### Option A — with `make` (recommended)

```bash
make dev   # docker compose up: backend + frontend with hot-reload
```

### Option B — raw commands

```bash
# Backend (one terminal)
cd backend
uv sync
uv run alembic upgrade head   # one-time (and after new migrations): seeds the app's user row
uv run main.py                # start the API — host/port/reload read from .env (see Configuration)
```

The `alembic upgrade head` step is not optional on a fresh database: the app's
startup guard refuses to boot if the configured `V1_USER_ID` has no matching
row in `users`, and the migrations seed that row. Equivalent: `make migrate`.

```bash
# Frontend (second terminal)
# First time only: install pnpm globally (no admin required on Windows)
npm install -g pnpm@11.21.0
# Windows: ensure %APPDATA%\npm is on PATH (PowerShell, no admin):
#   setx PATH "%PATH%;%APPDATA%\npm"
# Reopen the shell after.

cd frontend
pnpm install
pnpm dev
```

Backend on `http://localhost:8000`, frontend on `http://localhost:3000`.

### Configuration

Runtime config lives in a gitignored `.env` at the repo root (copy from [.env.example](.env.example)). Defaults are production-safe, so `.env` is optional for local use. The `API_*` vars are the **backend** server's — the Next.js frontend has its own config. `uv run main.py` reads `API_HOST` / `API_PORT` / `API_RELOAD`; `make backend` forces `--reload` for dev regardless.

| Var            | Default                             | Purpose                                                          |
| -------------- | ----------------------------------- | ---------------------------------------------------------------- |
| `API_HOST`     | `127.0.0.1`                         | Backend bind address. Set `0.0.0.0` to expose on the LAN.        |
| `API_PORT`     | `8000`                              | Backend API port — the frontend calls the API here.              |
| `API_RELOAD`   | `false`                             | Uvicorn auto-reload. Set `true` for local hot-reload.            |
| `LOG_FORMAT`   | `console`                           | Set `json` for structured logs in production.                   |
| `LOG_LEVEL`    | `info`                              | Level for app + bridged uvicorn logs. Not SQLAlchemy: it pins its own logger to WARNING at import. |
| `DATABASE_URL` | `sqlite:///./data/fin-tracker.db`   | SQLAlchemy URL — Postgres support planned.                       |
| `V1_USER_ID`   | seeded UUID                         | Must match the Alembic-seeded `users` row.                       |

### VS Code / IDE setup

1. After `uv sync` completes, select the project venv as Python interpreter so type hints and import resolution work:
   **`Ctrl+Shift+P` → "Python: Select Interpreter" → `backend/.venv/Scripts/python.exe`** (or `backend/.venv/bin/python` on macOS/Linux).
2. If `fastapi`, `sqlalchemy`, etc. still show as "missing import" after that, reload the window (`Ctrl+Shift+P` → "Developer: Reload Window").

## Development workflow

`pre-commit` runs `ruff` + `ty` + `eslint` + `tsc` on every commit. GitHub Actions runs the same checks plus the test suite on every push to `main` and on PRs.

Running from source (no Docker) and keeping dependencies current — pnpm, frontend, and backend update procedures — are documented in [docs/LOCAL_SETUP.md](docs/LOCAL_SETUP.md).

| Command          | What it does                              |
| ---------------- | ----------------------------------------- |
| `make dev`       | Full stack via docker compose             |
| `make test`      | `pytest` (backend) + coverage             |
| `make lint`      | `ruff check` + `eslint`                   |
| `make typecheck` | `ty` + `tsc --noEmit`                     |

Activate the git pre-commit hook locally after `uv sync`:

```bash
cd backend && uv run pre-commit install
```

## Roadmap

- One issuer, end-to-end: Axis CC PDF → parse → dedupe → tag → review screen.
- Add ICICI parser.
- Manual transactions + category management UI.
- Investment side: manual + transaction-CSV import + XIRR.
- Multi-currency + INDmoney import.
- Dashboards: live tiles, monthly-by-category, weekly/monthly bar, net-worth headline.
- Portfolio vs benchmark: scalar XIRR alpha against an Indian index-fund NAV/price snapshot.
- CSV export + Google Drive sync.
- Tax statements & reporting: capital-gains statement (FIFO STCG/LTCG split, debt-slab flag, ₹1.25L exemption) + dividend-income summary.
- Basic auth, cloud-deployable.
- OCR for scanned bills, recurring-transaction detection, budgets, multi-user households, capital-loss set-off + carry-forward, tax-loss harvesting, push-based live updates.

Full sequencing detail in [PRD.md § Build sequencing](PRD.md#build-sequencing--what-to-build-first).

## Troubleshooting

Five issues that bit during bootstrap on Windows. Documented here so the next person (or future-you) skips them.

**`uv sync` fails with TLS / certificate errors.**
Your shell may have a `UV_INDEX` env var (e.g. a corporate artifactory). This project's `[tool.uv]` in `backend/pyproject.toml` already pins to public PyPI, but the env var can override per-invocation. Force the project config: `UV_INDEX= uv sync`.

**VS Code says `fastapi` / `sqlalchemy` / etc. imports are missing.**
VS Code is using the system Python instead of the project venv. Fix: `Ctrl+Shift+P` → "Python: Select Interpreter" → pick `backend/.venv/Scripts/python.exe` (or `backend/.venv/bin/python` on macOS/Linux).

**`pnpm: command not found` after `npm install -g pnpm`.**
npm's global prefix on Windows (`%APPDATA%\npm`) isn't on PATH by default in bash / git-bash. Add it from PowerShell (no admin): `setx PATH "%PATH%;%APPDATA%\npm"`, then reopen the shell.

**`pnpm install` errors with `ERR_PNPM_IGNORED_BUILDS`.**
Already handled in this repo — `frontend/pnpm-workspace.yaml` opts `sharp` and `unrs-resolver` into running their install scripts. Re-run `pnpm install` and the postinstall scripts execute cleanly.

**`pnpm dev` / `pnpm typecheck` / `pnpm lint` fail with `'sh' is not recognized` (or `tsc` / `next` / `eslint` "not recognized").**
A partial `pnpm install` can skip generating the Windows `.cmd` / `.ps1` shims in `node_modules/.bin/`, leaving only the extension-less bash shims — which PowerShell/cmd can't run. Regenerate them: `corepack pnpm install --force`. To bypass without reinstalling, invoke the tool through Node directly:
```powershell
node node_modules/next/dist/bin/next dev          # instead of pnpm dev
node node_modules/typescript/bin/tsc --noEmit     # instead of pnpm typecheck
node node_modules/eslint/bin/eslint.js .          # instead of pnpm lint
```

## License

MIT — see [LICENSE](LICENSE).

## Author

Built by Ashutosh Upadhyay. Personal project.
