# Corporate TLS-proxy CA hook

Behind a corporate TLS-inspection proxy (e.g. Zscaler), outbound HTTPS presents a
certificate signed by a private root that neither the Docker image's trust store nor
Python's `certifi` bundle knows about. Two things in this repo break as a result:

1. **`next build`** fetches the fonts declared with `next/font/google` from Google at
   build time and fails with repeated `Error while requesting resource`.
2. **The F7 price refresh** (`POST /instruments/refresh-navs`, the sync button on
   `/holdings` and `/portfolio`) fails every feed with
   `[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate` — AMFI
   NAVAll and the Yahoo quotes alike. Same for `/fx` and `/benchmarks/refresh`.

Despite the `frontend/` path, this dir is the single on-disk home for that root CA;
both consumers read from here.

**If you build/run on a normal/home network or CI:** do nothing. This dir stays empty
(just these docs) and everything uses public roots.

**If you are behind a corporate proxy:** drop that proxy's root CA here as a PEM:

```
frontend/certs/corp-root-ca.crt
```

## Consumer 1 — the frontend Docker build

The frontend `Dockerfile` `base` stage adds every `*.crt` in this dir to the image
trust store before `next build`, so the font fetch validates. No further steps.

## Consumer 2 — the backend price feeds (local dev)

`httpx` verifies against `certifi`'s Mozilla roots, but honours `SSL_CERT_FILE` (via
`trust_env`, on by default at every call site). That variable **replaces** the trust
store rather than adding to it, so the corp root has to be concatenated *with* the
certifi roots — point `SSL_CERT_FILE` at the corp cert alone and every
non-intercepted host starts failing instead.

`make backend` does this for you: the `ca-bundle` target merges certifi's roots with
every `*.crt` here into the git-ignored `backend/data/corp-ca-bundle.pem` and exports
`SSL_CERT_FILE`. With no `*.crt` present the target is inert and the recipe is
identical to the proxy-less case.

Without `make` installed, do it by hand once —

```powershell
cd backend
cat "$(uv run python -c 'import certifi; print(certifi.where())')" ../frontend/certs/*.crt > data/corp-ca-bundle.pem
```

— then launch the backend through the git-ignored env file that points at it. `uv run`
injects those vars into the child process, which is what `httpx` reads:

```powershell
cd backend
uv run --env-file data/dev.env main.py
```

`main.py` is the entry point (it sources host/port/reload from the repo-root `.env`);
the repo-root `.env` itself is **not** a substitute, because `pydantic-settings` parses
it into `Settings` without ever touching `os.environ`, so `SSL_CERT_FILE` there is inert.

The variable is read once at process start, so a *running* server never picks up a
newly-created bundle — restart it through the command above.

Regenerate the bundle after a `certifi` upgrade, or the public half goes stale.

## This never leaves your machine

- `*.crt` here is git-ignored (see `.gitignore`) — it can't be committed. So are the
  generated `backend/data/corp-ca-bundle.pem` and `backend/data/dev.env` (`data/`).
- `scripts/package_source.py` explicitly strips cert files, so a source zip you hand
  to a non-corporate laptop is clean (the dir arrives empty of certs, and that laptop
  builds against public roots with no extra steps).
