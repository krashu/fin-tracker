# Local setup & dependency updates

Developer-facing playbook for running fin-tracker from source (no Docker) and for
updating its dependencies safely. If you just want to *run* the app, use the Docker
path in the [README](../README.md#deploy-with-docker) instead — this doc is for working
on the code and keeping deps current.

Pinning policy comes from [PRD.md § Maintenance posture](../PRD.md):

- **Backend** — bounded ranges in `backend/pyproject.toml`, `uv.lock` pins exact and is
  committed. `uv lock --upgrade` moves within ranges.
- **Frontend** — **exact-pinned** in `frontend/package.json` (no `^`/`~`), `pnpm-lock.yaml`
  committed.

---

## 1. Local install (from source)

### Prerequisites

- **Python 3.13** + **uv**
- **Node 24** (pinned in [`.nvmrc`](../.nvmrc) → `24.18.0`) + **pnpm 11** (via Corepack, below)
- *(Optional)* **make**

### Install the toolchain (Windows / winget)

```powershell
# uv (Python package/venv manager)
winget install astral-sh.uv

# Node (current LTS)
winget install OpenJS.NodeJS.LTS

# make (optional)
winget install ezwinports.make
```

Restart the shell (or `winget`-installed tools may not be on `PATH` yet) and verify:
`uv --version`, `node --version`, `make --version`.

**pnpm is *not* a separate install** — it ships with Node and is managed by **Corepack**
(see the frontend step below), which reads the exact version from `package.json`'s
`packageManager` field. Don't `winget install pnpm.pnpm` — a standalone pnpm fights
Corepack's version pinning.

> **Node version note:** `OpenJS.NodeJS.LTS` installs the current LTS (Node 24) — the major
> [`.nvmrc`](../.nvmrc) and CI target (`24.18.1`), so you're on the right line. If you want
> the exact patch, or juggle other projects on different Node majors, use **fnm** instead —
> `winget install Schniz.fnm`, then from a dir with `.nvmrc` run `fnm use --install-if-missing`.

**Other platforms:** uv → `pip install uv` or [astral.sh/uv](https://docs.astral.sh/uv/);
Node 24 → [fnm](https://github.com/Schniz/fnm) / nvm honoring `.nvmrc`; make → native on
Linux/macOS (`brew install make` if needed).

### Backend (one terminal)

```bash
cd backend
uv sync                        # creates backend/.venv and installs exactly from uv.lock
uv run alembic upgrade head    # one-time + after new migrations: seeds the V1 user row
uv run pre-commit install      # activate the ruff/ty/eslint/tsc git hook (once per clone)
uv run main.py                 # API on http://localhost:8000  (make backend forces --reload)
```

`uv sync` reads `pyproject.toml` + `uv.lock` and provisions `backend/.venv` — you don't
create or activate a venv yourself; prefix commands with `uv run`. It's idempotent, so
re-run it any time deps change.

`alembic upgrade head` is **not optional on a fresh DB** — the startup guard refuses to
boot if `V1_USER_ID` has no matching `users` row, and the migration seeds it.
Equivalent: `make migrate` (which also creates `backend/data/`).

`pre-commit install` wires the local gate so `ruff` + `ty` + `eslint` + `tsc` (and the
fixture-redaction test) run on every commit — matching CI. Run it from whichever directory
holds the active `.git`.

### Backend configuration

Runtime config is a **gitignored `.env` at the repo root** (copy from
[`.env.example`](../.env.example)). Defaults are local-safe, so `.env` is optional for
plain local dev. `uv run main.py` reads `API_HOST` / `API_PORT` / `API_RELOAD`;
`make backend` forces `--reload` regardless. Key vars (full table in the
[README](../README.md#configuration)):

| Var            | Default                           | Purpose                                       |
| -------------- | --------------------------------- | --------------------------------------------- |
| `API_PORT`     | `8000`                            | Backend API port — the frontend calls it here |
| `API_RELOAD`   | `false`                           | Set `true` for local hot-reload               |
| `DATABASE_URL` | `sqlite:///./data/fin-tracker.db` | SQLAlchemy URL (Postgres in v1.5+)            |
| `V1_USER_ID`   | seeded UUID                       | Must match the Alembic-seeded `users` row     |

### VS Code interpreter

After `uv sync`, point the editor at the project venv so imports resolve:
**`Ctrl+Shift+P` → "Python: Select Interpreter" → `backend/.venv/Scripts/python.exe`**
(`backend/.venv/bin/python` on macOS/Linux). If imports still show missing, reload the
window (`Ctrl+Shift+P` → "Developer: Reload Window").

> **Corp-network `uv sync` TLS failure:** if your shell has a `UV_INDEX` env var pointing
> at a corporate artifactory, it can override this project's public-PyPI pin per-invocation
> and fail with certificate errors. Force the project config: `UV_INDEX= uv sync`.

### Frontend (second terminal)

Prefer **Corepack** (bundled with Node) over a global `npm install -g pnpm` — it reads the
exact version from `package.json`'s `packageManager` field, so everyone runs the same pnpm:

```bash
corepack enable
corepack prepare pnpm@11.14.0 --activate   # matches packageManager in frontend/package.json
cd frontend
pnpm install
pnpm dev                                    # http://localhost:3000
```

> **Windows PATH note:** if you used `npm install -g pnpm` instead, `%APPDATA%\npm` may not
> be on PATH in bash/git-bash. Add it from PowerShell (no admin): `setx PATH "%PATH%;%APPDATA%\npm"`,
> then reopen the shell. See the README [Troubleshooting](../README.md#troubleshooting) section
> for the `'sh' is not recognized` shim issue (`corepack pnpm install --force`).

### Install the pre-commit gate (once)

```bash
uv run pre-commit install    # from whichever directory holds the active .git
```

After this the gate is automatic on every commit. `.pre-commit-config.yaml` runs 12 hooks:
file hygiene (trailing whitespace, EOF, YAML/TOML syntax, merge conflicts, line endings),
`ruff check --fix`, `ruff format`, **`ty check app`**, `eslint`, **`tsc --noEmit`**, and the
fixture-redaction test.

Both type-checkers run per-commit, so a type error blocks the commit rather than waiting for
CI. `make lint` / `make typecheck` stay the manual whole-repo commands.

### Verify the install

```bash
make lint         # ruff check + eslint
make typecheck    # ty + tsc --noEmit
make test         # pytest + coverage
```

---

## 2. Updating pnpm itself

pnpm's version is pinned in `frontend/package.json` → `"packageManager": "pnpm@<x.y.z>"`.
Corepack reads that field, so bumping the version there is what actually changes the pnpm
everyone uses.

```bash
cd frontend
pnpm --version                     # current
npm view pnpm version              # latest published

# 1. Edit packageManager in package.json to the new version, then:
corepack prepare pnpm@<new-version> --activate
pnpm --version                     # confirm it flipped
```

`frontend/package.json` is the only place the version is pinned; keep the prose copies in
sync when you bump it (`README.md` Quickstart, this doc's Corepack command).

---

## 3. Updating frontend packages

Frontend deps are **exact-pinned** — never introduce `^`/`~`, and never run
`pnpm add`/`pnpm up` unprompted (it rewrites pins and the lockfile). The safe procedure:

### Step 1 — see what's outdated

```bash
cd frontend
pnpm outdated
```

### Step 2 — sort updates into risk tiers

- **Safe** — patch/minor bumps *within the same major* (e.g. `next 16.2.6 → 16.2.10`,
  `react 19.2.6 → 19.2.7`). Apply these together.
- **Major** — a leading-number change (e.g. `zod 3 → 4`, `typescript 5 → 7`,
  `eslint 9 → 10`). Each carries real breakage; do them **one at a time on their own
  branch** with a full verify loop, not as part of a sweep.

Two recurring gotchas:

- **`@types/node` tracks the Node *runtime*, not "latest".** Node is pinned to 24.x
  (`.nvmrc`), so pin `@types/node` to the latest **24.x** — not the newest published major —
  or types drift from the runtime. Find it with `npm view "@types/node@24" version`.
- **`eslint-config-next`** must move in lockstep with `next` (same version), and it gates
  which major of `eslint` is supported.

### Step 3 — edit exact pins, then install

Hand-edit the exact versions in `package.json` (keep them exact — no range prefixes), then:

```bash
pnpm install       # regenerates pnpm-lock.yaml to match
```

### Step 4 — verify

```bash
pnpm typecheck     # tsc --noEmit
pnpm lint          # eslint
pnpm build         # next build — the real proof; exercises every route
```

All three must pass. `next build` is the decisive check — typecheck + lint alone won't
catch a runtime/route regression.

> **Prettier / CRLF false alarm:** `pnpm exec prettier --check` flags most files on a
> Windows checkout because `core.autocrlf=true` gives the working tree CRLF while prettier
> defaults to LF. Git stores LF, so committed content is clean — this is *not* caused by a
> dep bump and shouldn't be "fixed" with a repo-wide reformat.

### What ships

Only two files change for a frontend dep update: `frontend/package.json` and
`frontend/pnpm-lock.yaml`.

---

## 4. Updating backend packages

Backend uses **bounded ranges** (floor = current, ceiling = next breaking boundary) plus a
3-day `exclude-newer` cooldown in `backend/pyproject.toml`; `uv.lock` pins exact and is
committed.

```bash
cd backend
uv lock --upgrade      # move all deps to the newest allowed within the pyproject ranges
# — or a single package —
uv lock --upgrade-package <name>
uv sync                # apply the new lockfile to .venv
```

To cross a range ceiling (a genuine major bump), widen the range in `pyproject.toml`
first, then `uv lock`. Verify with `make test` + `make typecheck`, and surface the change
before committing.

`uv.lock`'s exact pins are always committed alongside the `pyproject.toml` change.
