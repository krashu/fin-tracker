# syntax=docker/dockerfile:1
# Dev-tools image for the VS Code dev container: backend (Python + uv) and frontend
# (Node + pnpm) in one place, so `make backend|frontend|test|lint|typecheck` and
# pre-commit run exactly as they do natively. NOT a runtime image; the shipped images
# are backend/Dockerfile and frontend/Dockerfile.
#
# Build context is the repo root, narrowed by Dockerfile.dockerignore (allowlist), so
# only the three files COPYed below are sent.
#
# Versions are read from the repo, not repeated here:
#   Node  <- .nvmrc
#   pnpm  <- frontend/package.json "packageManager"
# Python 3.14 comes from the base image (matches backend/Dockerfile and CI; pyproject
# allows >=3.13,<3.15). UV_PYTHON_DOWNLOADS=never makes uv use it instead of fetching.
# Keep the base tag in step with backend/.python-version: with downloads off, uv fails
# if that file asks for a minor the image doesn't have.
FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim

ENV DEBIAN_FRONTEND=noninteractive \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy

# ---- OS tools + corporate CA trust (same pattern as frontend/Dockerfile) -----------
# Any *.crt in frontend/certs/ joins the system store; frontend/certs/README.md.
COPY frontend/certs/ /tmp/corp-certs/
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl xz-utils git make libatomic1 \
    && find /tmp/corp-certs -name '*.crt' -exec cp {} /usr/local/share/ca-certificates/ \; \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/* /tmp/corp-certs
# uv/httpx read SSL_CERT_FILE; Node reads NODE_EXTRA_CA_CERTS.
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

# ---- Node, version from .nvmrc, checksum-verified ----------------------------------
# (libatomic1 above: the official tarball links against it; slim images lack it.)
COPY .nvmrc /tmp/.nvmrc
RUN set -eu; \
    v="$(tr -d '[:space:]' < /tmp/.nvmrc)"; v="${v#v}"; \
    case "$(dpkg --print-architecture)" in amd64) a=x64 ;; arm64) a=arm64 ;; *) exit 1 ;; esac; \
    f="node-v${v}-linux-${a}.tar.xz"; \
    cd /tmp; \
    curl -fsSLO "https://nodejs.org/dist/v${v}/${f}"; \
    curl -fsSLO "https://nodejs.org/dist/v${v}/SHASUMS256.txt"; \
    grep " ${f}\$" SHASUMS256.txt | sha256sum -c -; \
    tar -xJf "${f}" -C /usr/local --strip-components=1 --no-same-owner; \
    rm -f "${f}" SHASUMS256.txt /tmp/.nvmrc

# ---- pnpm, version from package.json's packageManager ------------------------------
COPY frontend/package.json /tmp/package.json
RUN npm install -g "pnpm@$(node -p "require('/tmp/package.json').packageManager.split('@')[1].split('+')[0]")" \
    && rm /tmp/package.json

# ---- non-root user ------------------------------------------------------------------
# The two mount points are created here, owned by `dev`, because Docker seeds a fresh
# named volume (the venv and node_modules volumes in devcontainer.json) from the image
# path, ownership included. Without this they come up root-owned and `uv sync` /
# `pnpm install` fail with EACCES.
RUN useradd -m -u 1000 -s /bin/bash dev \
    && mkdir -p /workspaces/fintracker/backend/.venv /workspaces/fintracker/frontend/node_modules \
    && chown -R dev:dev /workspaces
# node_modules is on a named volume, a different filesystem from $HOME. pnpm then puts
# its store beside the project (.pnpm-store/ in the repo, untracked, thousands of files).
# Pin it to $HOME instead; pnpm falls back to copying when it can't hardlink.
# pnpm 11 reads pnpm_config_*; the old npm_config_store_dir is ignored.
ENV pnpm_config_store_dir=/home/dev/.local/share/pnpm/store
# The base image ships the stdlib without .pyc files, and `dev` can't write its root-owned
# __pycache__ dirs, so every Python process recompiled it (~1.4s each, per xdist worker).
RUN python -m compileall -q /usr/local/lib/python3.14
USER dev
WORKDIR /workspaces/fintracker
