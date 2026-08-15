# fin-tracker backend — FastAPI + uv, Python 3.13.
#
# Multi-stage on purpose: uv builds the venv in the `builder` stage, then the lean `run`
# stage copies ONLY /app (venv + source) onto the plain official Python image. That drops
# two things the runtime never needs — the ~52 MB uv/uvx binaries and the ~125 MB uv
# download cache (which lives at /root/.cache/uv, outside /app, so the copy leaves it
# behind). We run uvicorn straight from the venv interpreter instead of `uv run`, so uv is
# absent from the shipped image entirely.
#
# Safe because the uv builder image (astral-sh/uv:python3.13-bookworm-slim) IS the official
# python:3.13-slim-bookworm + uv: same interpreter at /usr/local/bin/python3.13, so the
# venv's symlinks/shebangs resolve unchanged in the run stage (identical WORKDIR /app).
#
# The whole backend/ tree is copied (respecting .dockerignore), which lands the `alembic/`
# script tree + `alembic.ini` at /app/alembic — required because app.core.migrations runs
# `alembic upgrade head` in-process on boot, resolving the script location as /app/alembic.

# ---- builder: uv installs the venv, then is discarded --------------------------
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

# UV_COMPILE_BYTECODE: precompile .pyc for faster cold starts.
# UV_LINK_MODE=copy: silence the hardlink warning when the cache and target sit on
# different filesystems (the norm in a container build).
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Layer 1 — dependencies only, cached until pyproject.toml / uv.lock change.
# --no-install-project: the app itself isn't installed here, just its deps.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Layer 2 — application source (+ alembic tree), then finalize the venv.
COPY . .
RUN uv sync --frozen --no-dev

# ---- run: lean runtime — no uv binary, no download cache -----------------------
FROM python:3.14-slim-bookworm AS run

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copies .venv (~151 MB) + source. The uv cache (/root/.cache/uv) and the uv binaries
# live outside /app and are left behind — that's the whole point of the split.
COPY --from=builder /app /app

# Non-root hardening. Fixed uid so a native-Linux operator can chown the ./data bind mount
# to a stable value (see docker-compose.yml). No `chown -R /app` needed: appuser only READS
# /app (world-readable) and executes the venv; the ONLY path it writes is the /data volume
# (SQLite DB + WAL/journal), which the bind mount owns at runtime.
RUN useradd -u 10001 --create-home appuser
USER appuser

EXPOSE 8000

# No --reload: production/self-host default. The dev override swaps this command.
# --host 0.0.0.0 so the published port is reachable from the host. Runs the venv interpreter
# directly (uv is gone); the relative path resolves against WORKDIR /app.
CMD [".venv/bin/python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
