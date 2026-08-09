# fin-tracker frontend — Next.js 16 (App Router), Node 24 (build 24.18.1, run 24.18.0),
# pnpm via corepack (11.14.0, resolved from package.json's `packageManager`).
#
# The Node major matches the rest of the repo — .nvmrc (24.18.1),
# .github/workflows/ci.yml (node-version "24"), PRD §Production-grade essentials
# and §Maintenance posture, and frontend/package.json's "@types/node": "24.13.3".
# That alignment is the point: @types/node is the drift vector, so server-rendered
# code is typechecked against the same API surface the deployed artefact executes.
# This image previously built on Node 22 and ran on distroless nodejs22, which meant
# a Node-24-only runtime API passed CI and then threw inside a run stage that has no
# shell to diagnose it. Keep all four in step on the next bump: the `base` FROM, the
# `run` stage's distroless tag, .nvmrc, and this header.
#
# The Debian codename is part of the contract, not incidental. Distroless no longer
# publishes nodejs*-debian12, so `run` is nodejs24-debian13 (trixie) and `base` is
# node:24.18.1-trixie-slim so the two still share a libc by construction — see the
# `run` stage note, which is what that invariant protects.
#
# The two stages agree on the major but not the patch: distroless tracks upstream
# patches on its own cadence and nodejs24-debian13 currently ships 24.18.0 against the
# base's 24.18.1. Only the major is load-bearing (it is what makes @types/node honest),
# so this is tolerated — but don't write a patch-exact claim here without re-checking.
# Verified 2026-08-02 in the built image: run reports v24.18.0, glibc 2.41, and the
# traced sharp 0.34.5 native addon loads.
#
# Corepack is bundled in Node 24 and is NOT bundled from Node 25 on (the TSC voted to
# stop distributing it). `corepack enable` below breaks on the next LTS — install
# corepack explicitly or pin pnpm another way when that lands.
#
# Multi-stage so the production runner (`run`) and the dev hot-reload container
# (`dev`) build from ONE Dockerfile:
#   base  — node + pnpm + TLS trust store (see below)
#   deps  — full dependency install (incl. the `next` CLI + dev deps)
#   build — production `next build` (standalone output) with the API URL baked in
#   dev   — hot-reload target selected by docker-compose.override.yml
#   run   — lean standalone runtime (default target)
#
# NEXT_PUBLIC_* is inlined at build time, so the API base URL is a build ARG here,
# NOT a runtime env var. Direct-mode default is the absolute host URL; the proxy
# overlay overrides it to a relative "/api/v1" (same-origin) via build.args.

# ---- base: node + pnpm + CA trust ----------------------------------------------
# `next build` (Turbopack) fetches the fonts declared via next/font/google and
# verifies TLS against the OS trust store — which the slim base does NOT ship. So we
# install ca-certificates (public roots) unconditionally; the build fails without it
# on ANY network. Additionally, any *.crt dropped into ./certs/ (gitignored) is added
# to the store, so the build also works behind a corporate TLS-inspection proxy
# (e.g. Zscaler) whose root CA isn't publicly trusted. Empty certs/ = plain build.
FROM node:24.18.1-trixie-slim AS base
ENV PNPM_HOME=/pnpm PATH=/pnpm:$PATH
RUN corepack enable
COPY certs/ /tmp/corp-certs/
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates; \
    if ls /tmp/corp-certs/*.crt >/dev/null 2>&1; then \
      cp /tmp/corp-certs/*.crt /usr/local/share/ca-certificates/; \
    fi; \
    update-ca-certificates; \
    rm -rf /var/lib/apt/lists/* /tmp/corp-certs
# native-tls (Turbopack) reads SSL_CERT_FILE; Node reads NODE_EXTRA_CA_CERTS.
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt \
    NEXT_TELEMETRY_DISABLED=1
WORKDIR /app

# ---- deps: full install, cached until the lockfile changes ---------------------
FROM base AS deps
COPY package.json pnpm-lock.yaml ./
# --ignore-scripts: skip dependency build scripts (sharp, unrs-resolver). pnpm 11
# treats *unapproved* ignored builds as a hard error non-interactively; opting out
# both silences that and matches the host. Next ships prebuilt platform binaries as
# regular deps and the app's own `pnpm build` runs separately, so nothing is lost.
RUN pnpm install --frozen-lockfile --ignore-scripts

# ---- build: production standalone bundle ---------------------------------------
FROM base AS build
COPY --from=deps /app/node_modules ./node_modules
ARG NEXT_PUBLIC_API_BASE_URL=http://localhost:8000/api/v1
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
COPY . .
RUN pnpm build

# ---- dev: hot-reload target (compose override selects this via build.target) ----
# Off `deps` (so `next` CLI + dev deps + the CA trust are present); the populated
# node_modules here seed the override's anonymous /app/node_modules volume, so
# `pnpm dev` starts with no manual install. Source is bind-mounted over /app at runtime.
FROM deps AS dev
EXPOSE 3000
CMD ["pnpm", "dev"]

# ---- run: lean production runtime (distroless) ---------------------------------
# distroless nodejs24-debian13 shares the SAME trixie base as the `build` stage where
# Next traced sharp-linux-x64, so its glibc/libstdc++/libgcc match by construction — the
# native addon stays loadable (and is never invoked anyway; the app uses no next/image, so
# sharp is require()d lazily only on a /_next/image hit that never happens). No shell/apt/
# yarn/npm — ~80 MB lighter than node:24-trixie-slim (151 vs 230 MB, measured 2026-08-02).
# :nonroot runs as uid 65532. The standalone runtime writes nothing to disk (no ISR/
# revalidate); --chown below is cheap insurance so an eager .next/cache mkdir can't EACCES.
# ENTRYPOINT is already ["/nodejs/bin/node"], so CMD is just the script (NOT ["node", ...]).
# HOSTNAME=0.0.0.0 is required — Next's standalone server.js binds process.env.HOSTNAME,
# and without it the published :3000 port isn't reachable from the host.
FROM gcr.io/distroless/nodejs24-debian13:nonroot AS run
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
# Standalone output carries its own minimal node_modules + server.js. `public/`
# does not exist in this project, so it is intentionally not copied.
COPY --from=build --chown=65532:65532 /app/.next/standalone ./
COPY --from=build --chown=65532:65532 /app/.next/static ./.next/static
EXPOSE 3000
CMD ["server.js"]
