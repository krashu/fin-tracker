"""structlog configuration + PII masking processor.

One call to :func:`configure_logging` at application startup wires a single
canonical pipeline: app loggers (via ``structlog.stdlib.LoggerFactory`` +
``BoundLogger``) and foreign stdlib loggers (uvicorn / SQLAlchemy / Starlette's
default exception handler) both flow through one
``structlog.stdlib.ProcessorFormatter`` on the root handler, where
:func:`mask_pii` and the renderer run exactly once. Nothing bypasses the
pipeline, so every log line is structured + PII-masked.

``format_exc_info`` runs in the shared pre-chain *before* :func:`mask_pii` so
PII embedded in exception messages (e.g. ``ValueError(f"bad PAN {pan}")``) gets
scrubbed too — but only for the classes the free-text regexes actually cover,
which is PAN and 16-digit cards. A bare account number in an exception string is
**not** scrubbed; see :func:`mask_pii`'s own scope note below.

Renderer is env-driven: ``ConsoleRenderer`` by default (local dev),
``JSONRenderer`` when ``LOG_FORMAT=json``. Verbosity is ``LOG_LEVEL``-driven
(default INFO), governing app logs and the bridged uvicorn loggers. It does
**not** govern SQLAlchemy: SQLAlchemy pins its own ``sqlalchemy`` logger to
WARNING at import time, and ``sqlalchemy.engine`` inherits that, so lowering the
root level alone will never surface SQL. Reach for ``echo=True`` on the engine
(or an explicit ``setLevel`` on that logger) when you want query logs.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, cast

import structlog

from app.core.config import get_settings
from app.core.pii_patterns import CARD_RE, PAN_RE

# Name tag on the root handler we own, so configure_logging() can replace just
# our handler on repeat calls without disturbing handlers installed by others
# (e.g. pytest's log-capture handler).
_ROOT_HANDLER_NAME = "fin_tracker_root"

# Field names whose values are masked verbatim. Matching is case-insensitive
# on the key. Per PRD §Production-grade essentials, the masked value is flat
# ``"***"`` — emitting last-4 would itself violate "no card last-4 in logs".
# (Cited against the PRD rather than CLAUDE.md: the latter is gitignored, so a
# reference to it dangles for anyone else reading this file.)
_PII_KEYS: frozenset[str] = frozenset(
    {
        "pan",
        "card_number",
        "card_no",
        "card_last4",
        "account_number",
        "account_no",
    }
)


def _scrub(value: Any) -> Any:
    """Recurse into dicts/lists, replacing PII-key values with ``"***"``."""
    if isinstance(value, dict):
        return {k: ("***" if k.lower() in _PII_KEYS else _scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def mask_pii(
    _logger: Any, _method_name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Mask PII in structured fields and rendered exception strings.

    Mask scope is intentionally narrow and explicit:

    1. **Field-name match** (recursive into dicts + lists): keys matching
       :data:`_PII_KEYS` (case-insensitive) → value ``"***"``. Values
       under unrelated keys (e.g. ``description="ABCDE1234F"``) pass
       through unscrubbed — bind PII through the right key name or
       accept the leak. Pinned by
       ``test_pii_pass_through_under_innocuous_key``.
    2. **Free-text regex** (PAN + 16-digit card only, via
       :mod:`app.core.pii_patterns`) on the ``event``, ``exception`` +
       ``stack`` strings. ``format_exc_info`` and ``StackInfoRenderer``
       upstream render these into strings before this processor runs.
       ``event`` is scanned because bridged foreign records (uvicorn /
       SQLAlchemy) carry their message there; app ``event`` values are
       static names ("request_completed") that never match the regex.

    Out of scope (per PRD §Production-grade essentials): email,
    phone numbers, 13/15-digit card forms, lowercase PAN, and free-text
    account numbers (no account-number regex — those are masked by key
    name only).
    """
    for key in list(event_dict.keys()):
        value = event_dict[key]
        if key.lower() in _PII_KEYS:
            event_dict[key] = "***"
        elif isinstance(value, (dict, list)):
            event_dict[key] = _scrub(value)

    for key in ("event", "exception", "stack"):
        text = event_dict.get(key)
        if isinstance(text, str):
            text = PAN_RE.sub("***", text)
            text = CARD_RE.sub("***", text)
            event_dict[key] = text

    return event_dict


def _resolve_level(name: str) -> int:
    """Map a ``LOG_LEVEL`` string to a stdlib level int; unknown → INFO."""
    return logging.getLevelNamesMapping().get(name.strip().upper(), logging.INFO)


def configure_logging() -> None:
    """Wire the canonical logging pipeline. Idempotent — safe to call repeatedly.

    App loggers end at ``wrap_for_formatter`` (no renderer in the structlog
    chain) and hand off to a single ``ProcessorFormatter`` on the root handler;
    foreign stdlib records reach the same formatter via ``foreign_pre_chain``.
    ``mask_pii`` + the renderer live only in the formatter, so each event is
    masked and rendered exactly once regardless of origin.
    """
    settings = get_settings()
    level = _resolve_level(settings.log_level)
    use_json = settings.log_format.lower() == "json"
    renderer: Any = (
        structlog.processors.JSONRenderer() if use_json else structlog.dev.ConsoleRenderer()
    )

    # Shared pre-renderer processors — run once per event: in the native chain
    # for app logs, as foreign_pre_chain for stdlib logs.
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,  # `logger` field: source module (app + foreign)
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        # structlog's own default. Caching would freeze a module-level logger's
        # processor list at first bind, so structlog.testing.capture_logs() in
        # tests can't intercept it after a reconfigure (a recurring footgun).
        # Re-binding per call is negligible for this single-user, low-log app.
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            mask_pii,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.set_name(_ROOT_HANDLER_NAME)
    root = logging.getLogger()
    # Idempotent: drop our previously-installed handler (configure_logging()
    # runs in the app lifespan and again per-test) but leave any other root
    # handler (e.g. pytest's log-capture handler) untouched. Appending blindly
    # would stack handlers and emit duplicate lines.
    root.handlers[:] = [h for h in root.handlers if h.get_name() != _ROOT_HANDLER_NAME]
    root.addHandler(handler)
    root.setLevel(level)
    # uvicorn installs its own handlers at startup (before the lifespan runs);
    # clear them and force propagation so those records reach our root handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy.engine"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structlog logger. Use ``get_logger(__name__)`` once per module.

    ``structlog.get_logger()`` returns a lazy proxy (typed ``Any``) that binds to
    this app's ``wrapper_class`` (``structlog.stdlib.BoundLogger``) on first use;
    the ``cast`` pins that concrete type so callers get typed ``.info`` / ``.bind``
    etc. The emitted ``logger`` field (via ``add_logger_name``) is the module path.
    """
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))
