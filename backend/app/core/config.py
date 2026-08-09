"""Application settings backed by pydantic-settings.

Reads from the repo-root ``.env`` file. Keys not declared on :class:`Settings`
are ignored (the same ``.env`` is shared with the parser-test password vars,
which aren't app settings).

Used by :mod:`app.core.db` for the engine URL and (later) by Alembic's ``env.py``
so production migrations target the same database the running app uses.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from uuid import UUID

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# parents[3]: config.py → core/ → app/ → backend/ → repo root.
# Brittle to layout changes; revisit if app/ ever moves.
# In the Docker image the backend is copied to /app (so this resolves to `/`, and
# `/.env` simply doesn't exist → pydantic-settings skips the missing file). That's
# by design: the compose stack injects settings as real env vars, which take
# precedence over the env_file anyway.
REPO_ROOT = Path(__file__).resolve().parents[3]

# Fixed API mount prefix. Single source of truth: main.py mounts the v1 router
# here and app/api/v1/auth.py derives its refresh-cookie path from it. Lives in
# this leaf module (both importers already import get_settings from here) so the
# auth router doesn't import app.api.v1.router — that would be a router↔auth cycle.
API_V1_PREFIX = "/api/v1"

# Loud dev placeholder for jwt_secret; rejected at settings-validation time when
# cookie_secure is on (a hosted https deploy must set a real JWT_SECRET).
_DEV_JWT_SECRET = "dev-insecure-secret-change-me-before-any-deploy"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str = Field(
        default="sqlite:///./data/fin-tracker.db",
        validation_alias="DATABASE_URL",
        description=(
            "SQLAlchemy URL. SQLite for v1 (single-user, local-first); swap to "
            "postgresql+psycopg://... for v2 — declarative models stay portable."
        ),
    )
    apply_migrations_on_startup: bool = Field(
        default=True,
        validation_alias="APPLY_MIGRATIONS_ON_STARTUP",
        description=(
            "Run `alembic upgrade head` in the app lifespan on boot. True (default) "
            "suits local-first single-user dev: a migration pulled from `main` applies "
            "on the next `make backend` instead of 500-ing on a stale table. Set false "
            "for the v2 hosted deployment, where migrations run as a discrete deploy step. "
            "Forced false in the test suite (tests build schema via create_all)."
        ),
    )
    seed_demo_on_startup: bool = Field(
        default=True,
        validation_alias="SEED_DEMO_ON_STARTUP",
        description=(
            "Seed the demo dataset (sample accounts / spending / investments) in the app "
            "lifespan when the DB is empty. True (default) suits local-first dev: a fresh "
            "`make backend` comes up with a populated app and no separate seed step. Set "
            "false for the self-host stacks (real data) so a fresh ./data volume stays "
            "empty; forced false in the test suite. Only ever fires on an empty DB — never "
            "re-seeds or touches a DB that already has accounts/transactions."
        ),
    )
    v1_user_id: UUID = Field(
        default=UUID("00000000-0000-0000-0000-000000000001"),
        validation_alias="V1_USER_ID",
        description=(
            "Fixed UUID for the seeded demo / v1 data-owner row (migration 0001 seeds "
            "it, 0017 stamps the demo credentials on it). No longer read on the request "
            "path — auth now resolves the user from the access-token cookie. Still the "
            "anchor for the lifespan boot-guard and the test fixtures, so don't drop it."
        ),
    )
    jwt_secret: str = Field(
        default=_DEV_JWT_SECRET,
        validation_alias="JWT_SECRET",
        description=(
            "HS256 signing key for access-token JWTs (PRD §Users & access v2). The "
            "default is a loud dev placeholder — set a real random secret via JWT_SECRET "
            "in .env before any non-local deploy. Rotating it invalidates all live access "
            "tokens (refresh tokens survive — they're opaque DB rows, not JWTs)."
        ),
    )
    access_token_ttl_minutes: int = Field(
        default=15,
        gt=0,
        validation_alias="ACCESS_TOKEN_TTL_MINUTES",
        description="Access-token lifetime. Short by design — refresh rotates a new one.",
    )
    refresh_token_ttl_days: int = Field(
        default=14,
        gt=0,
        validation_alias="REFRESH_TOKEN_TTL_DAYS",
        description="Refresh-token (sessions row) lifetime; the sliding-session window.",
    )
    session_absolute_ttl_hours: int = Field(
        default=12,
        gt=0,
        validation_alias="SESSION_ABSOLUTE_TTL_HOURS",
        description=(
            "Absolute session lifetime cap (OWASP absolute timeout). The refresh TTL bounds "
            "*idle* lifetime (the sliding window); this bounds *total* lifetime from login — a "
            "refresh family can't be rotated past this age no matter how active, so a stolen / "
            "replayed refresh token can't be extended indefinitely. Enforced server-side in "
            "auth_service.rotate_session against the family's origin (its first-issued row)."
        ),
    )
    cookie_secure: bool = Field(
        default=False,
        validation_alias="COOKIE_SECURE",
        description=(
            "Set the Secure flag on auth cookies. False for local http dev; MUST be true "
            "in any hosted (https) deployment so cookies never ride a plaintext connection."
        ),
    )
    demo_login_enabled: bool = Field(
        default=False,
        validation_alias="DEMO_LOGIN_ENABLED",
        description=(
            "Accept the source-published demo credentials (app.core.demo) at POST /auth/login. "
            "OFF by default: the demo row is stamped onto the fixed-UUID user by migration 0017 "
            "on EVERY stack (SEED_DEMO_ON_STARTUP gates the demo dataset, not the demo account), "
            "and on an upgraded or dev-seeded install that row owns the real data. Turn it on "
            "only where a public password is acceptable — a dev box, or a throwaway showcase. "
            "Necessary but not sufficient: see :attr:`demo_login_permitted`."
        ),
    )
    cookie_samesite: str = Field(
        default="lax",
        validation_alias="COOKIE_SAMESITE",
        description=(
            "SameSite policy for auth cookies ('lax' | 'strict' | 'none'). 'lax' works for "
            "same-site dev (frontend + API both on localhost). Cross-site hosting needs "
            "'none' + Secure. Paired with a fail-closed Origin check for CSRF defense."
        ),
    )
    rate_limit_enabled: bool = Field(
        default=True,
        validation_alias="RATE_LIMIT_ENABLED",
        description=(
            "Enable the in-process auth-endpoint rate limiter (register/login/refresh). "
            "On by default — argon2id is deliberately expensive, so an unthrottled login "
            "is both a brute-force and a CPU-DoS surface."
        ),
    )
    auth_rate_limit_per_minute: int = Field(
        default=20,
        gt=0,
        validation_alias="AUTH_RATE_LIMIT_PER_MINUTE",
        description="Max register/login/refresh attempts per client IP per 60s window.",
    )
    rate_limit_trust_proxy: bool = Field(
        default=False,
        validation_alias="RATE_LIMIT_TRUST_PROXY",
        description=(
            "Key the auth rate limiter on the client IP from X-Forwarded-For instead of the "
            "immediate peer. OFF by default (direct-mode default is byte-identical). Enable ONLY "
            "behind a trusted single-hop reverse proxy (the docker-compose.proxy.yml overlay): "
            "otherwise every client collapses to the proxy IP and shares one bucket. SECURITY: "
            "when true the backend port MUST NOT be directly reachable — a client that can hit the "
            "API directly could spoof X-Forwarded-For to dodge throttling (main.py logs a warning)."
        ),
    )
    cors_allowed_origins: str = Field(
        default="http://localhost:3000",
        validation_alias="CORS_ALLOWED_ORIGINS",
        description=(
            "Comma-separated exact origins allowed to call the API with credentials "
            "(cookies). Read by main.py for CORS + the fail-closed Origin CSRF check. "
            "No wildcard — credentialed CORS forbids it. Add the prod frontend origin here."
        ),
    )
    host: str = Field(
        default="127.0.0.1",
        validation_alias="API_HOST",
        description=(
            "Backend API bind address, read by main.py. 127.0.0.1 keeps the API "
            "local-only (v1 default); set API_HOST=0.0.0.0 to expose it on the LAN for "
            "the v1.5 hosted deployment. (The Next.js frontend's host/port are its own "
            "config, not this .env.)"
        ),
    )
    port: int = Field(
        default=8000,
        validation_alias="API_PORT",
        description=(
            "Backend API port, read by main.py. The Next.js frontend calls the API at "
            ":8000 (see frontend/lib/api); move both together if you change it."
        ),
    )
    reload: bool = Field(
        default=False,
        validation_alias="API_RELOAD",
        description=(
            "Uvicorn auto-reload for the backend, read by main.py. Production-safe "
            "default (off); set API_RELOAD=true in .env for local dev, or use "
            "`make backend`, which forces --reload regardless."
        ),
    )
    log_format: str = Field(
        default="console",
        validation_alias="LOG_FORMAT",
        description=(
            "structlog renderer, read by configure_logging(). 'console' (default) is "
            "the human-readable dev renderer; 'json' emits structured logs (v2 hosted "
            "deployment)."
        ),
    )
    log_level: str = Field(
        default="info",
        validation_alias="LOG_LEVEL",
        description=(
            "Root log level for structlog + bridged stdlib logs (uvicorn), read by "
            "configure_logging(). Does NOT reach SQLAlchemy: it pins its own 'sqlalchemy' "
            "logger to WARNING at import time, so engine/SQL logs need echo=True or an "
            "explicit setLevel, not this knob. Unknown values fall back to INFO."
        ),
    )
    amfi_navall_url: str = Field(
        default="https://portal.amfiindia.com/spages/NAVAll.txt",
        validation_alias="AMFI_NAVALL_URL",
        description=(
            "AMFI's daily all-schemes NAV file (PRD §F7 NAV snapshot). Public, no key. "
            "Read by the refresh-navs endpoint and passed to nav_snapshot_service."
        ),
    )
    yahoo_quote_base_url: str = Field(
        default="https://query1.finance.yahoo.com/v8/finance/chart",
        validation_alias="YAHOO_QUOTE_BASE_URL",
        description=(
            "Yahoo Finance v8 chart base URL for Indian-equity quotes (PRD §F7). The "
            "snapshot appends '/<symbol>.NS' (NSE) or '.BO' (BSE). Public, no key."
        ),
    )
    nav_fetch_timeout_secs: float = Field(
        default=5.0,
        validation_alias="NAV_FETCH_TIMEOUT_SECS",
        description=(
            "Per-request HTTP timeout for the NAV/price snapshot. Tight on purpose: a "
            "slow symbol becomes a fetch-error (skipped) rather than a multi-minute hang."
        ),
    )
    mfapi_base_url: str = Field(
        default="https://api.mfapi.in/mf",
        validation_alias="MFAPI_BASE_URL",
        description=(
            "mfapi.in base URL for mutual-fund NAV *history* (PRD §F8 view 5 benchmark "
            "backfill). Public, no key. benchmark_service appends '/<scheme_code>'. "
            "Seed-time only — never on the GET /portfolio/performance hot path."
        ),
    )
    frankfurter_base_url: str = Field(
        default="https://api.frankfurter.app",
        validation_alias="FRANKFURTER_BASE_URL",
        description=(
            "frankfurter.app base URL for USD→INR FX rates (PRD §F7 FX layer). Public, no key; "
            "exchangerate.host is the documented drop-in fallback (swap the URL, no code change). "
            "fx_service appends '/latest', '/<date>', or '/<start>..<end>'. Seed-time backfill "
            "only — the holdings/portfolio/ingest reads touch only the cached fx_rates rows."
        ),
    )
    fx_fetch_timeout_secs: float = Field(
        default=5.0,
        validation_alias="FX_FETCH_TIMEOUT_SECS",
        description=(
            "Per-request HTTP timeout for the FX backfill. Tight on purpose: a slow/unreachable "
            "source becomes a counted warning rather than a multi-minute hang."
        ),
    )

    @property
    def cors_origins(self) -> list[str]:
        """Parsed, de-blanked list form of ``cors_allowed_origins``."""
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def demo_login_permitted(self) -> bool:
        """May the demo credentials authenticate? Explicitly opted in AND plain http.

        The single expression read by BOTH the login path
        (:func:`app.services.auth_service.authenticate`) and the public
        ``GET /auth/config``, so the "Try the demo" button and the login 401 can never
        disagree — the invariant ADR-0003 Alternative 3 was defending.

        Two necessary conditions, because neither works alone. ``cookie_secure`` alone
        was the shipped gate and it is structurally unsettable on the documented LAN
        self-host topology (``deploy/Caddyfile`` serves ``:80`` plain http and browsers
        drop ``Secure`` cookies over http, so enabling it breaks login outright) — which
        left a source-published password permanently open on every LAN-reachable stack.
        ``demo_login_enabled`` alone would let a hardened https deploy re-open it, which
        the shipped gate correctly refused. ANDing them is strictly the safer of the two.
        """
        return self.demo_login_enabled and not self.cookie_secure

    @model_validator(mode="after")
    def _validate_cookie_policy(self) -> Settings:
        """Fail loud on cookie settings a browser would silently reject."""
        samesite = self.cookie_samesite.lower()
        if samesite not in {"lax", "strict", "none"}:
            raise ValueError("COOKIE_SAMESITE must be one of: lax, strict, none")
        if samesite == "none" and not self.cookie_secure:
            raise ValueError(
                "COOKIE_SAMESITE=none requires COOKIE_SECURE=true — browsers drop "
                "insecure SameSite=None cookies, which would silently break auth."
            )
        return self

    @model_validator(mode="after")
    def _validate_auth_secrets(self) -> Settings:
        """Fail loud on hosting footguns the added credentialed-auth surface invites."""
        if self.cookie_secure and self.jwt_secret == _DEV_JWT_SECRET:
            raise ValueError(
                "JWT_SECRET is still the dev placeholder while COOKIE_SECURE=true — a "
                "hosted deploy must set a real random JWT_SECRET (the default key is "
                "public, so tokens signed with it are forgeable)."
            )
        # allow_credentials=True forbids a wildcard origin (the browser would drop the
        # credentialed response), and an empty allowlist bricks both CORS and the CSRF
        # Origin check. Reject both up-front rather than fail silently at request time.
        origins = self.cors_origins
        if not origins:
            raise ValueError("CORS_ALLOWED_ORIGINS must list at least one origin.")
        if "*" in origins:
            raise ValueError(
                "CORS_ALLOWED_ORIGINS cannot be '*' — credentialed CORS forbids a "
                "wildcard; list explicit origins."
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor — re-reads only on interpreter restart."""
    return Settings()
