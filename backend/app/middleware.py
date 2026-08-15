"""HTTP request-logging middleware.

Emits one canonical wide event per request via ``structlog``:
``request_completed`` on a normal return, ``request_failed`` on an
unhandled exception. Fields: ``method``, ``path``, ``route`` (the matched
template, e.g. ``/api/v1/transactions/{id}``, or ``None`` on a 404),
``status_code``, ``duration_ms``, ``client_ip``, ``user_id``.
``request_id`` and ``user_id`` are bound into structlog's contextvars
before ``call_next`` so downstream service/parser logs in the same request
inherit them automatically.

The propagation is **forward-only**: fields a route handler binds with
``logger.bind(...)`` flow into that handler's logs, but they do NOT flow
back into this middleware's wide event because Starlette's
:class:`BaseHTTPMiddleware` dispatches ``call_next`` on a separate anyio
task. Acceptable for v1 — revisit with a raw ASGI middleware if a use
case actually depends on handler-bound fields appearing in the wide
event.
"""

from __future__ import annotations

import re
import time
import uuid

import structlog
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import get_settings
from app.core.log_config import get_logger
from app.core.security import ACCESS_COOKIE_NAME, decode_access_token

# Client-supplied ``X-Request-ID`` values are accepted only if they match
# this shape. CRLF or other header-injection payloads fall through to a
# server-generated UUID.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _resolve_request_id(request: Request) -> str:
    raw = request.headers.get("x-request-id") or request.headers.get("request-id")
    if raw is not None and _REQUEST_ID_RE.fullmatch(raw):
        return raw
    return uuid.uuid4().hex


def _route_template(request: Request) -> str | None:
    """The matched route template (low-cardinality), e.g. ``/api/v1/imports/{batch_id}``.

    FastAPI 0.137+ (the ``_IncludedRouter`` refactor, PR #15745) exposes only the
    *route-local* template on ``scope['route']`` — e.g. ``/imports/{batch_id}`` —
    with the ``/api/v1`` ``include_router`` prefix in neither ``path_format`` nor
    ``root_path`` (it survives only in the real URL). The usual ecosystem trick
    (walk ``app.routes`` + ``route.matches`` → ``route.path``) returns ``None`` on
    0.137 because included routes are wrapped in a prefix-less ``_IncludedRouter``,
    so we rebuild the template positionally: graft the local template's segments
    back onto the URL's prefix.

    LOAD-BEARING INVARIANT: every router / ``include_router`` prefix is a static
    literal (no path param in a *prefix* segment) and no route declares a trailing
    slash — both hold today. ``test_wide_event_route_keeps_param_placeholder``
    fails loudly if a parametrized prefix sneaks in (it would otherwise emit a
    high-cardinality value instead of ``{...}``). ``None`` when nothing matched
    (404) or the request failed before routing.
    """
    route = request.scope.get("route")
    if route is None:
        return None
    local = getattr(route, "path_format", None) or getattr(route, "path", None)
    if not isinstance(local, str):
        return None
    local_segs = [s for s in local.split("/") if s]
    path_segs = [s for s in request.url.path.split("/") if s]
    cut = len(path_segs) - len(local_segs)
    prefix_segs = path_segs[:cut] if cut > 0 else []
    return "/" + "/".join(prefix_segs + local_segs)


def _resolve_user_id(request: Request) -> str:
    """Decode the access cookie for logging; 'anonymous' when absent/invalid."""
    token = request.cookies.get(ACCESS_COOKIE_NAME)
    if token:
        uid = decode_access_token(token)
        if uid is not None:
            return str(uid)
    return "anonymous"


def _level_for_status(status_code: int) -> str:
    if status_code >= 500:
        return "error"
    if status_code >= 400:
        return "warning"
    return "info"


class HTTPLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Per-request fetch so the logger always binds under the current config.
        # Cheap — logger caching is off (see app.core.log_config).
        logger = get_logger(__name__)
        request_id = _resolve_request_id(request)
        client_ip = request.client.host if request.client else None
        # Best-effort user id for the wide event: decode the access cookie
        # without raising (an unauthenticated request — login/register/health —
        # logs user_id="anonymous" rather than 500ing on a missing token).
        user_id = _resolve_user_id(request)
        start = time.perf_counter()

        with structlog.contextvars.bound_contextvars(request_id=request_id, user_id=user_id):
            try:
                response = await call_next(request)
            except Exception:
                duration_ms = round((time.perf_counter() - start) * 1000, 2)
                logger.error(
                    "request_failed",
                    request_id=request_id,
                    user_id=user_id,
                    method=request.method,
                    path=request.url.path,
                    route=_route_template(request),
                    status_code=500,
                    duration_ms=duration_ms,
                    client_ip=client_ip,
                    exc_info=True,
                )
                raise

            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            response.headers["X-Request-ID"] = request_id
            level = _level_for_status(response.status_code)
            # request_id / user_id are also bound into contextvars above so
            # downstream handler logs inherit them via ``merge_contextvars``;
            # passing them here as well makes the middleware's wide event
            # self-contained regardless of whether ``merge_contextvars`` is in
            # the chain (e.g. ``structlog.testing.capture_logs`` strips it).
            getattr(logger, level)(
                "request_completed",
                request_id=request_id,
                user_id=user_id,
                method=request.method,
                path=request.url.path,
                route=_route_template(request),
                status_code=response.status_code,
                duration_ms=duration_ms,
                client_ip=client_ip,
            )
            return response


# Methods that never mutate state — exempt from the Origin check (a GET carries
# no CSRF risk, and OPTIONS preflight is handled by CORSMiddleware above this).
_CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class OriginCSRFMiddleware(BaseHTTPMiddleware):
    """Fail-closed Origin check for state-changing requests (PRD §Users & access v2).

    Auth cookies are ``SameSite`` (primary CSRF defense); this is the belt to that
    braces. Every non-safe method must carry an ``Origin`` header in the configured
    allowlist — a missing or foreign Origin is rejected 403. Fail-closed on missing
    Origin is deliberate: a cross-site form POST can omit Origin, so allowing the
    absent case would reopen the hole. Browser fetches from the SPA always send
    Origin; server-to-server callers must set it explicitly.

    Mounted INNER to CORSMiddleware so preflight never reaches here and a 403 still
    gets CORS headers on the way out.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method not in _CSRF_SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is None:
                return JSONResponse({"detail": "origin not allowed"}, status_code=403)
            # Allow configured CORS origins or same-origin requests (e.g. unified full-stack SPA)
            host = request.headers.get("host")
            proto = request.headers.get("x-forwarded-proto", request.url.scheme)
            same_origin = f"{proto}://{host}" if host else None
            if origin not in get_settings().cors_origins and origin != same_origin:
                return JSONResponse({"detail": "origin not allowed"}, status_code=403)
        return await call_next(request)


# Static hardening headers stamped on every response. Deliberately conservative for a
# JSON API + same-origin SPA that is never framed and sniffs nothing:
#   - nosniff        — block MIME-type sniffing.
#   - DENY / CSP     — clickjacking: the API is never embedded in a frame. X-Frame-Options
#                      for legacy browsers, frame-ancestors 'none' (the quoted keyword is
#                      required or the directive parse-fails) for modern ones.
#   - Referrer-Policy — don't leak full URLs cross-origin.
# HSTS is NOT here — it's conditional (see `apply_security_headers`).
SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}

# max-age 1 year, no includeSubDomains (a single-user self-host rarely owns a subdomain tree
# and it could force HTTPS onto unrelated sibling services), no preload.
_HSTS_HEADER = "Strict-Transport-Security"
_HSTS_VALUE = "max-age=31536000"


def apply_security_headers(response: Response) -> None:
    """Stamp the static security headers + conditional HSTS onto ``response``.

    Shared by :class:`SecurityHeadersMiddleware` (the normal path, including the CSRF 403
    the inner middleware returns) and the catch-all 500 handler in ``app.main`` — the 500
    handler is dispatched by Starlette's ``ServerErrorMiddleware``, which sits *outside*
    this middleware, so an unhandled-error response would otherwise ship with none of these.
    HSTS rides only a hardened https deploy (``cookie_secure``): sending it over the
    local-http dev / ``up`` deploy would poison the browser's HSTS cache for ``localhost``.
    """
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    if get_settings().cookie_secure:
        response.headers[_HSTS_HEADER] = _HSTS_VALUE


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Stamp hardening headers on every non-error response (PRD §Production-grade essentials).

    Mounted OUTERMOST of the user middleware so it also covers the CSRF 403 from
    ``OriginCSRFMiddleware``. The unhandled-500 path is handled separately in ``app.main``
    (see :func:`apply_security_headers`) because that response is generated above this stack.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        apply_security_headers(response)
        return response
