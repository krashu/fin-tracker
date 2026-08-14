"""Tests for :mod:`app.middleware` + :func:`app.core.log_config.mask_pii`.

Two halves:

* ``test_response_*`` / ``test_log_event_*`` / ``test_status_to_level_*`` /
  ``test_request_id_propagates_*`` — middleware behaviour through the
  FastAPI app. Use :func:`structlog.testing.capture_logs` to observe the
  emitted event dict before the renderer runs.
* ``test_pii_processor_*`` — direct unit tests on :func:`mask_pii`.
  ``capture_logs`` swaps the processor chain entirely, so masking has to
  be exercised against the function directly.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from io import StringIO

import pytest
import structlog
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.log_config import configure_logging, get_logger, mask_pii
from app.main import app


@pytest.fixture
def reset_logging() -> Iterator[None]:
    """Restore the default logging config after a test mutates LOG_* env.

    Listed BEFORE ``monkeypatch`` in the test signature so its teardown runs
    *after* monkeypatch reverts the env var (LIFO finalizers) — i.e. the
    restoring ``configure_logging()`` reads the clean environment."""
    yield
    get_settings.cache_clear()
    configure_logging()


@contextmanager
def _capture_with_contextvars() -> Iterator[list[dict[str, object]]]:
    """Variant of ``structlog.testing.capture_logs`` that keeps
    ``merge_contextvars`` in the processor chain so contextvar bindings
    (e.g. the middleware's ``request_id``) flow into the captured
    event_dict. The stock ``capture_logs`` strips all processors, which
    hides forward-propagation behaviour we want to assert."""
    cap = structlog.testing.LogCapture()
    saved = structlog.get_config()["processors"]
    structlog.configure(processors=[structlog.contextvars.merge_contextvars, cap])
    try:
        yield cap.entries
    finally:
        structlog.configure(processors=saved)


# UUID4 hex = 32 lowercase hex chars; what the middleware generates when
# no client-supplied ``X-Request-ID`` matches ``_REQUEST_ID_RE``.
_UUID_HEX_RE = re.compile(r"^[0-9a-f]{32}$")


# --------------------------- request_id wiring ----------------------------


def test_response_echoes_request_id_when_valid(client: TestClient) -> None:
    resp = client.get("/api/v1/health", headers={"X-Request-ID": "trace-abc-123"})
    assert resp.status_code == 200
    assert resp.headers["X-Request-ID"] == "trace-abc-123"


def test_response_generates_request_id_when_absent(client: TestClient) -> None:
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert _UUID_HEX_RE.fullmatch(resp.headers["X-Request-ID"])


def test_response_regenerates_request_id_when_invalid(client: TestClient) -> None:
    # Space is outside the [A-Za-z0-9_-]{1,64} allowed shape — middleware
    # must drop the supplied value and generate a fresh UUID hex.
    resp = client.get("/api/v1/health", headers={"X-Request-ID": "bad value!"})
    assert resp.status_code == 200
    assert resp.headers["X-Request-ID"] != "bad value!"
    assert _UUID_HEX_RE.fullmatch(resp.headers["X-Request-ID"])


def test_response_regenerates_request_id_with_trailing_newline(client: TestClient) -> None:
    # Regression: ``re.match`` against ``^...$`` accepts a trailing ``\n`` ($ matches
    # just before a single trailing LF), so a newline-bearing id would slip the guard
    # and taint both the echoed response header and the wide-event ``request_id`` field
    # (header-injection / log-forging). ``_resolve_request_id`` uses ``fullmatch`` to
    # reject it. The "bad value!" case above can't catch this — both match/fullmatch
    # reject the space.
    resp = client.get("/api/v1/health", headers={"X-Request-ID": "abc\n"})
    assert resp.status_code == 200
    assert resp.headers["X-Request-ID"] != "abc\n"
    assert "\n" not in resp.headers["X-Request-ID"]
    assert _UUID_HEX_RE.fullmatch(resp.headers["X-Request-ID"])


# --------------------------- log event shape ------------------------------


def test_log_event_has_expected_fields(client: TestClient) -> None:
    with structlog.testing.capture_logs() as logs:
        resp = client.get("/api/v1/health")
    completed = [e for e in logs if e.get("event") == "request_completed"]
    assert len(completed) == 1
    event = completed[0]
    assert event["method"] == "GET"
    assert event["path"] == "/api/v1/health"
    assert event["status_code"] == 200
    assert isinstance(event["duration_ms"], float)
    assert event["request_id"] == resp.headers["X-Request-ID"]


# --------------------------- status → level -------------------------------


def test_status_to_level_2xx_info(client: TestClient) -> None:
    with structlog.testing.capture_logs() as logs:
        client.get("/api/v1/health")
    completed = [e for e in logs if e.get("event") == "request_completed"]
    assert completed[0]["log_level"] == "info"


def test_status_to_level_4xx_warning(client: TestClient) -> None:
    with structlog.testing.capture_logs() as logs:
        client.get("/api/v1/does-not-exist")
    completed = [e for e in logs if e.get("event") == "request_completed"]
    assert completed[0]["status_code"] == 404
    assert completed[0]["log_level"] == "warning"


@pytest.fixture
def boom_route() -> Iterator[str]:
    """Mount a temporary route that raises, so the middleware's
    ``request_failed`` branch can be exercised. Removed on teardown."""
    path = "/__boom_test__"

    async def _boom() -> None:
        raise RuntimeError("forced 500 for middleware test")

    app.add_api_route(path, _boom, methods=["GET"])
    try:
        yield path
    finally:
        app.routes[:] = [r for r in app.routes if getattr(r, "path", None) != path]


def test_status_to_level_5xx_error(boom_route: str) -> None:
    # raise_server_exceptions=False so TestClient returns the framework
    # 500 instead of bubbling the RuntimeError up into the test thread.
    #
    # Constructed WITHOUT ``with`` on purpose: the lifespan would run app.main's
    # V1_USER_ID guard against the real ``SessionLocal`` — this test takes no DB
    # fixture, so that hits the dev database and fails with "no such table: users"
    # whenever it isn't migrated. The middleware under test runs regardless of
    # lifespan, and ``configure_logging()`` is already applied by the autouse
    # fixture in tests/conftest.py. Same pattern as tests/api/test_health.py.
    c = TestClient(app, raise_server_exceptions=False)
    with structlog.testing.capture_logs() as logs:
        resp = c.get(boom_route)
    assert resp.status_code == 500
    failed = [e for e in logs if e.get("event") == "request_failed"]
    assert len(failed) == 1
    event = failed[0]
    assert event["log_level"] == "error"
    assert event["status_code"] == 500
    assert event["path"] == boom_route


# ---------------------- forward request_id propagation --------------------


@pytest.fixture
def logging_route() -> Iterator[str]:
    """Mount a route that emits a structlog event from inside the handler,
    so we can verify the middleware's ``request_id`` flows forward into
    handler-scope logs via structlog's contextvars."""
    path = "/__log_test__"

    async def _emit() -> dict[str, bool]:
        structlog.get_logger("test.handler").info("handler_event")
        return {"ok": True}

    app.add_api_route(path, _emit, methods=["GET"])
    try:
        yield path
    finally:
        app.routes[:] = [r for r in app.routes if getattr(r, "path", None) != path]


def test_request_id_propagates_into_handler_log(logging_route: str) -> None:
    # _capture_with_contextvars keeps ``merge_contextvars`` in the chain so
    # the handler's log inherits ``request_id`` from the middleware's
    # contextvar binding (which is exactly what we're verifying).
    #
    # No ``with`` on the client — see test_status_to_level_5xx_error above.
    c = TestClient(app)
    with _capture_with_contextvars() as logs:
        resp = c.get(logging_route)
    handler = [e for e in logs if e.get("event") == "handler_event"]
    completed = [e for e in logs if e.get("event") == "request_completed"]
    assert len(handler) == 1
    assert len(completed) == 1
    assert handler[0]["request_id"] == completed[0]["request_id"]
    assert handler[0]["request_id"] == resp.headers["X-Request-ID"]


# ------------------------- mask_pii unit tests ----------------------------


def test_pii_processor_masks_known_keys() -> None:
    assert mask_pii(None, "info", {"pan": "ABCDE1234F", "other": "x"}) == {
        "pan": "***",
        "other": "x",
    }
    assert mask_pii(None, "info", {"account_number": "12345678"}) == {"account_number": "***"}
    assert mask_pii(None, "info", {"card_last4": "1234"}) == {"card_last4": "***"}


def test_pii_processor_recurses_into_nested_dicts_and_lists() -> None:
    assert mask_pii(None, "info", {"payload": {"card_number": "4111111111111111"}}) == {
        "payload": {"card_number": "***"}
    }
    assert mask_pii(None, "info", {"items": [{"pan": "ABCDE1234F"}, {"pan": "FGHIJ5678K"}]}) == {
        "items": [{"pan": "***"}, {"pan": "***"}]
    }


def test_pii_processor_scrubs_pan_in_exception_string() -> None:
    out = mask_pii(None, "info", {"exception": "ValueError: bad PAN ABCDE1234F here"})
    assert "ABCDE1234F" not in out["exception"]
    assert "***" in out["exception"]


def test_pii_processor_scrubs_card_number_in_exception_string() -> None:
    out = mask_pii(None, "info", {"exception": "card 4111111111111111 failed"})
    assert "4111111111111111" not in out["exception"]
    assert "***" in out["exception"]


def test_pii_processor_passes_through_unrelated_fields() -> None:
    payload = {"event": "hi", "duration_ms": 1.5, "status_code": 200, "method": "GET"}
    # Copy so the assertion compares to the original shape, not a mutated one
    assert mask_pii(None, "info", dict(payload)) == payload


def test_pii_processor_scrubs_hyphenated_card_in_exception_string() -> None:
    out = mask_pii(None, "info", {"exception": "card 4111-1111-1111-1111 declined"})
    assert "4111-1111-1111-1111" not in out["exception"]
    assert "***" in out["exception"]


def test_pii_processor_scrubs_stack_field() -> None:
    out = mask_pii(None, "info", {"stack": "Traceback ... PAN ABCDE1234F at line 42"})
    assert "ABCDE1234F" not in out["stack"]
    assert "***" in out["stack"]


def test_pii_pass_through_under_innocuous_key() -> None:
    # Failing-by-design tripwire. mask_pii is intentionally narrow: PII embedded
    # in values under unrelated keys (e.g. ``description``) passes through. If
    # someone widens mask_pii to sweep every string value, this test breaks and
    # forces a deliberate scope-contract change rather than silent widening.
    payload = {"description": "user PAN is ABCDE1234F"}
    assert mask_pii(None, "info", dict(payload)) == payload


def test_configure_logging_is_idempotent() -> None:
    from app.core.log_config import configure_logging

    configure_logging()
    first = len(structlog.get_config()["processors"])
    configure_logging()
    second = len(structlog.get_config()["processors"])
    assert first == second
    # Sanity: a fresh logger still works after the second configure.
    structlog.get_logger("test.idempotent").info("hello")


def test_mask_pii_runs_after_format_exc_info() -> None:
    # Pins the chain ordering invariant: format_exc_info must render exc_info
    # into the ``exception`` string BEFORE mask_pii scans it for PAN / card
    # patterns; reversed ordering would silently leak PII. In the canonical
    # pipeline mask_pii lives in the root handler's ProcessorFormatter while
    # format_exc_info runs upstream — in the native chain (app logs) and the
    # formatter's foreign_pre_chain (foreign logs). Assert across both homes.
    configure_logging()

    native = structlog.get_config()["processors"]
    assert structlog.processors.format_exc_info in native  # app path: exc rendered upstream

    fmt = logging.getLogger().handlers[-1].formatter
    assert isinstance(fmt, structlog.stdlib.ProcessorFormatter)
    # foreign path: exc rendered in the pre-chain, before the formatter's procs
    assert structlog.processors.format_exc_info in fmt.foreign_pre_chain
    # mask_pii is in the formatter, before the (last) renderer
    assert mask_pii in fmt.processors
    assert fmt.processors.index(mask_pii) < len(fmt.processors) - 1


# ----------------------- stdlib bridge / level knob -----------------------


def test_foreign_stdlib_log_is_bridged_and_masked(
    reset_logging: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A foreign (uvicorn/SQLAlchemy) record must reach our root handler,
    # render as JSON, and have PAN / card scrubbed from its message body.
    monkeypatch.setenv("LOG_FORMAT", "json")
    get_settings.cache_clear()
    configure_logging()

    root = logging.getLogger()
    buf = StringIO()
    sink = logging.StreamHandler(buf)
    sink.setFormatter(root.handlers[-1].formatter)  # reuse the bridge's formatter
    root.addHandler(sink)
    try:
        logging.getLogger("uvicorn.access").error("GET /x PAN ABCDE1234F card 4111111111111111")
    finally:
        root.removeHandler(sink)

    raw = buf.getvalue().strip()
    payload = json.loads(raw)  # proves JSON rendering on the bridged path
    assert payload["level"] == "error"
    assert "timestamp" in payload
    assert payload["logger"] == "uvicorn.access"  # add_logger_name on the foreign path
    assert "ABCDE1234F" not in raw
    assert "4111111111111111" not in raw
    assert "***" in payload["event"]


def test_app_log_carries_logger_name(reset_logging: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # get_logger(__name__) + add_logger_name: an app event carries its source in
    # the `logger` field. (Rendered-output path — capture_logs strips the chain.)
    monkeypatch.setenv("LOG_FORMAT", "json")
    get_settings.cache_clear()
    configure_logging()

    root = logging.getLogger()
    buf = StringIO()
    sink = logging.StreamHandler(buf)
    sink.setFormatter(root.handlers[-1].formatter)
    root.addHandler(sink)
    try:
        get_logger("app.sample.module").info("sample_event")
    finally:
        root.removeHandler(sink)

    payload = json.loads(buf.getvalue().strip())
    assert payload["logger"] == "app.sample.module"
    assert payload["event"] == "sample_event"


def test_log_level_env_override(reset_logging: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "warning")
    get_settings.cache_clear()
    configure_logging()

    root = logging.getLogger()
    assert root.level == logging.WARNING
    assert not root.isEnabledFor(logging.INFO)
    assert root.isEnabledFor(logging.WARNING)


def test_configure_logging_installs_single_root_handler() -> None:
    # Idempotency surface moved to root.handlers; the named handler must stay a
    # singleton across repeated configure_logging() calls (lifespan + per-test).
    configure_logging()
    configure_logging()
    ours = [h for h in logging.getLogger().handlers if h.get_name() == "fin_tracker_root"]
    assert len(ours) == 1


def test_capture_logs_sees_logger_bound_before_reconfigure() -> None:
    # Regression guard for the module-level-logger capture bug: a logger bound
    # (and, under cache_logger_on_first_use=True, cached) before a reconfigure
    # must still be observable by capture_logs(). configure_logging() rebinds
    # the processor list to a NEW instance, which orphaned a cached logger's
    # frozen reference. With caching OFF this passes; flipping the config back
    # to cache_logger_on_first_use=True makes this fail — by design.
    configure_logging()
    lg = structlog.get_logger("test.prebound")
    lg.info("warm")  # first bind, before the reconfigure below
    configure_logging()  # swaps _CONFIG.default_processors to a new list instance
    with structlog.testing.capture_logs() as logs:
        lg.info("evt")
    assert any(e.get("event") == "evt" for e in logs)


# ----------------------- wide-event PRD fields ----------------------------


def test_wide_event_carries_user_id_and_route(client: TestClient) -> None:
    with structlog.testing.capture_logs() as logs:
        client.get("/api/v1/health")
    completed = [e for e in logs if e.get("event") == "request_completed"]
    assert len(completed) == 1
    assert completed[0]["route"] == "/api/v1/health"
    assert completed[0]["user_id"] == str(get_settings().v1_user_id)


def test_wide_event_route_is_none_on_404(client: TestClient) -> None:
    with structlog.testing.capture_logs() as logs:
        client.get("/api/v1/does-not-exist")
    completed = [e for e in logs if e.get("event") == "request_completed"]
    assert completed[0]["status_code"] == 404
    assert completed[0]["route"] is None
    assert completed[0]["path"] == "/api/v1/does-not-exist"


def test_wide_event_route_keeps_param_placeholder(client: TestClient) -> None:
    # Low-cardinality guard: a parametrized route must log the TEMPLATE
    # ({batch_id}), never the concrete id. Pins the include_router-prefix
    # reconstruction in middleware._route_template — FastAPI 0.137+ exposes only
    # the route-LOCAL path on scope['route'], so the /api/v1 prefix is rebuilt
    # from the URL. A parametrized *prefix* (or a trailing-slash route) would
    # break that reconstruction; this fails loudly if it ever does.
    with structlog.testing.capture_logs() as logs:
        client.get("/api/v1/imports/999999/candidates")  # int param, matched route, 404 body
    completed = [e for e in logs if e.get("event") == "request_completed"]
    assert len(completed) == 1
    assert completed[0]["route"] == "/api/v1/imports/{batch_id}/candidates"
    assert "999999" not in completed[0]["route"]
