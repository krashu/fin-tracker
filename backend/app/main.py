"""FastAPI application entrypoint.

Mounts the v1 router under ``/api/v1``, wires the HTTP request-logging
middleware, and runs a lifespan handler that (a) calls
:func:`configure_logging` and (b) refuses to boot if the configured
``V1_USER_ID`` has no matching row in the ``users`` table. The latter
catches env-override drift (someone sets ``V1_USER_ID=<other-uuid>`` in
``.env`` but the migration seeded
``00000000-0000-0000-0000-000000000001``) at startup, not on the first
FK violation in a request handler.

Tests construct :class:`TestClient` with the context-manager form so the
lifespan runs (see :mod:`tests.api.conftest`); the api conftest
monkeypatches ``SessionLocal`` to point at the in-memory test engine so
the V1_USER_ID guard hits the seeded test DB, not production.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.api.v1.router import api_router
from app.core.config import API_V1_PREFIX, Settings, get_settings
from app.core.db import SessionLocal
from app.core.log_config import configure_logging, get_logger
from app.core.migrations import run_migrations
from app.middleware import (
    HTTPLoggingMiddleware,
    OriginCSRFMiddleware,
    SecurityHeadersMiddleware,
    apply_security_headers,
)
from app.models import User
from app.services.demo_seed import seed_demo_data

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings = get_settings()
    if settings.rate_limit_trust_proxy:
        # Trusting X-Forwarded-For is only safe when the backend port is unreachable except
        # through the trusted proxy; a directly-reachable API lets a client spoof XFF to dodge
        # throttling. We can't detect port exposure from in-process — surface an advisory.
        logger.warning(
            "rate_limit_trust_proxy_enabled",
            detail="keying the rate limiter on X-Forwarded-For; ensure the backend port is not "
            "directly reachable (only via the trusted reverse proxy)",
        )
    if (
        settings.demo_login_enabled
        and settings.cookie_secure
        and not settings.allow_demo_login_over_https
    ):
        # demo_login_permitted ANDs the two, so the flag is inert here unless explicitly
        # permitted over HTTPS. Say so: the operator asked for the demo login and would
        # otherwise get silence plus a "Try the demo" button that 401s.
        logger.warning(
            "demo_login_enabled_but_inert",
            detail="DEMO_LOGIN_ENABLED is set while COOKIE_SECURE=true; the demo login stays "
            "refused (a source-published password must not ride a hardened deploy). "
            "Set ALLOW_DEMO_LOGIN_OVER_HTTPS=true to enable demo login for a public showcase.",
        )
    elif settings.demo_login_enabled and settings.allow_demo_login_over_https:
        logger.info(
            "demo_login_allowed_over_https",
            detail="demo login is permitted over HTTPS (ALLOW_DEMO_LOGIN_OVER_HTTPS=true)",
        )
    if not settings.registration_enabled:
        logger.info(
            "registration_disabled",
            detail="open user registration is disabled on this instance "
            "(REGISTRATION_ENABLED=false)",
        )
    # Bring the DB to head before the guard below — migrations seed the v1
    # user row, so this must run first for a freshly created database.
    run_migrations(settings)
    with SessionLocal() as session:
        exists = session.scalar(select(User.id).where(User.id == settings.v1_user_id))
    if exists is None:
        raise RuntimeError(
            f"V1_USER_ID={settings.v1_user_id} has no matching row in `users`. "
            "Run `make migrate` to seed it, or align V1_USER_ID with the seeded UUID."
        )
    _maybe_seed_demo(settings)
    yield


def _maybe_seed_demo(settings: Settings) -> None:
    """Sync the demo dataset into place when SEED_DEMO_ON_STARTUP is enabled.

    Runs on EVERY boot, not just once on an empty DB:
    :func:`app.services.demo_seed.seed_demo_data` find-or-creates the demo
    accounts/instruments and wipes + regenerates the demo accounts'
    transactions for a rolling window ending at ``clock.today()`` (see that
    module's docstring for why a full wipe-and-regenerate is safe here — this
    account carries only seed data). So a `main.py` restart is what keeps the
    "Try the demo" account looking current, with no separate reseed step.
    Gated off for the self-host stacks (real data) and the test suite via
    SEED_DEMO_ON_STARTUP. A seed failure is logged, not swallowed silently, and
    re-raised — a broken dataset is a code bug to surface at boot.
    """
    if not settings.seed_demo_on_startup:
        return
    user_id = settings.v1_user_id
    with SessionLocal() as session:
        try:
            seeded = seed_demo_data(session, user_id=user_id)
        except Exception:
            session.rollback()
            logger.exception("demo_seed_failed")
            raise
    logger.info(
        "demo_seed_applied",
        accounts=seeded.accounts,
        transactions=seeded.transactions,
        instruments=seeded.instruments,
        investment_transactions=seeded.investment_transactions,
    )


# Config-driven CORS allowlist (CORS_ALLOWED_ORIGINS). Credentialed CORS forbids
# a wildcard, so origins are explicit. One source of truth — the 500 handler and
# the CSRF middleware read the same setting.
_CORS_ALLOWED_ORIGINS = get_settings().cors_origins


async def _cors_aware_500_handler(request: Request, exc: Exception) -> JSONResponse:
    """Stamp CORS headers on the catch-all 500 response.

    Starlette's ``ServerErrorMiddleware`` — which converts an unhandled
    exception into this 500 — sits *outside* ``CORSMiddleware``, so without
    this handler the error response ships with no CORS headers. The browser
    then reports a misleading CORS failure ("is the API running?") instead of
    surfacing the real 500. Echoing the allowed origin here lets a genuine
    server error reach the frontend *as* a 500.

    ``Access-Control-Allow-Credentials: true`` MUST accompany the origin echo:
    the frontend sends cookies (``credentials: include``), and a credentialed
    response without this header is rejected by the browser as a CORS error —
    the exact trap this handler exists to avoid. The exception is still
    re-raised after the handler returns, so ``TestClient`` and server-side
    logging are unaffected.
    """
    response = JSONResponse({"detail": "Internal Server Error"}, status_code=500)
    origin = request.headers.get("origin")
    if origin in _CORS_ALLOWED_ORIGINS:
        response.headers["access-control-allow-origin"] = origin
        response.headers["access-control-allow-credentials"] = "true"
        response.headers["vary"] = "Origin"
    # This 500 is generated by ServerErrorMiddleware, which sits OUTSIDE
    # SecurityHeadersMiddleware, so the response never travels back through it —
    # stamp the hardening headers here too (shared helper keeps the two paths in sync).
    apply_security_headers(response)
    return response


app = FastAPI(title="fin-tracker", version="0.1.0", lifespan=lifespan)
# Middleware add-order is LIFO — the last add is outermost. Desired stack
# (outer→inner): security-headers → CORS → logging → CSRF → app, so CORS handles
# preflight + always stamps headers, the CSRF 403 still gets CORS headers on the way
# out, and the security-headers layer wraps everything so it also stamps the CSRF 403.
# allow_credentials=True: the frontend authenticates via httpOnly cookies.
app.add_middleware(OriginCSRFMiddleware)
app.add_middleware(HTTPLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SecurityHeadersMiddleware)
app.add_exception_handler(Exception, _cors_aware_500_handler)
app.include_router(api_router, prefix=API_V1_PREFIX)

# Mount static frontend for full-stack deployment (e.g. FastAPI Cloud) if dist exists.
_frontend_dist_setting = get_settings().frontend_dist_dir
_frontend_path: Path | None = None
if _frontend_dist_setting:
    _p = Path(_frontend_dist_setting)
    _frontend_path = _p if _p.is_absolute() else (Path(__file__).resolve().parent.parent / _p)
else:
    _default_dist = Path(__file__).resolve().parent.parent / "frontend_dist"
    if _default_dist.is_dir() and any(_default_dist.iterdir()):
        _frontend_path = _default_dist

if _frontend_path and _frontend_path.is_dir() and hasattr(app, "frontend"):
    app.frontend("/", directory=str(_frontend_path), check_dir=False)
