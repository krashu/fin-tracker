# Fin Tracker — local dev commands.
# Stubs — filled in as milestones progress.

.PHONY: dev up up-proxy backend frontend test test-fast lint typecheck refresh-skills migrate ca-bundle

# Corporate TLS-proxy CA hook — LOCAL DEV ONLY. See frontend/certs/README.md.
# Behind a TLS-inspecting proxy (e.g. Zscaler) the F7 price feeds fail with
# CERTIFICATE_VERIFY_FAILED: httpx verifies against certifi's Mozilla roots, and the
# proxy presents a chain signed by a private root that isn't in them. Any *.crt in
# frontend/certs/ is concatenated with those roots into a git-ignored bundle, exported
# as SSL_CERT_FILE — which httpx honours via trust_env. Merged, not substituted:
# SSL_CERT_FILE *replaces* the trust store, so certifi's roots must ride along or
# every non-intercepted host breaks instead.
#
# Inert on a clean machine: no *.crt => BACKEND_ENV is empty, ca-bundle does nothing,
# and the `backend` recipe is identical to the proxy-less case. Nothing here reaches a
# source zip — the certs and the generated bundle are git-ignored, and
# scripts/package_source.py strips cert suffixes regardless.
CORP_CA_CERTS := $(wildcard frontend/certs/*.crt)
CA_BUNDLE     := backend/data/corp-ca-bundle.pem
BACKEND_ENV   := $(if $(CORP_CA_CERTS),SSL_CERT_FILE=$(abspath $(CA_BUNDLE)),)

refresh-skills:
	@python scripts/refresh_skills.py

# Regenerate on every `make backend` so a certifi upgrade can't leave stale roots.
ca-bundle:
ifneq ($(CORP_CA_CERTS),)
	@mkdir -p backend/data
	@cat "$$(cd backend && uv run python -c 'import certifi; print(certifi.where())')" \
	  $(CORP_CA_CERTS) > $(CA_BUNDLE)
	@echo "CA bundle: certifi roots + $(words $(CORP_CA_CERTS)) local cert(s) -> $(CA_BUNDLE)"
endif

# Dev hot-reload stack (base + docker-compose.override.yml auto-merge).
dev:
	docker compose up --build

# Local self-host stack — direct two-port (proxy-less). Baked localhost API URL is
# host-machine-only. --build so a changed build arg never reuses a stale image.
up:
	docker compose -f docker-compose.yml up --build

# Opt-in single-origin variant (Caddy reverse proxy; browse http://localhost:$${PROXY_PORT:-8080}).
up-proxy:
	docker compose -f docker-compose.yml -f docker-compose.proxy.yml up --build

migrate:
	@mkdir -p backend/data
	cd backend && uv run alembic upgrade head

backend: refresh-skills ca-bundle
	cd backend && $(BACKEND_ENV) uv run uvicorn app.main:app --reload

frontend:
	cd frontend && pnpm dev

# `-n auto` lives here rather than in pyproject's addopts because xdist costs ~17s of
# worker startup: worth it across the whole suite, a pure loss on the single-file runs
# pre-commit and debugging do. Drop to `-n0` to debug (no `--pdb` under xdist).
test: refresh-skills
	cd backend && uv run pytest -n auto

# Inner-loop suite: drops coverage instrumentation (~25% of the wall clock), the
# real-PDF tests and the `slow` marker. NOT the gate — `make test` is, and only it
# enforces the 75% coverage floor (pre-commit runs the redaction test alone). Use
# the full one before committing, and whenever the change is to a migration: the
# deselected test_migrations_stairway is what proves every downgrade reversible.
test-fast: refresh-skills
	cd backend && uv run pytest --no-cov -m "not real_pdf and not slow" -n auto

lint:
	cd backend && uv run ruff check .
	cd frontend && pnpm lint

typecheck:
	cd backend && uv run python -m ty check app
	cd frontend && pnpm typecheck
