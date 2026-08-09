"""CORS middleware tests.

The Next.js dev frontend runs on a separate origin (:3000) from the API
(:8000), so every browser fetch is cross-origin and needs CORS headers.
Without them the /expenses board can't load (the browser blocks the
response). These tests pin the allowlist so a regression surfaces here
rather than as a silent "stuck on Loading…" in the UI.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import app

ALLOWED = "http://localhost:3000"


@pytest.fixture
def boom_route() -> Iterator[str]:
    """Mount a route that raises so the catch-all 500 path is exercised."""
    path = "/api/v1/__cors_boom__"

    async def _boom() -> None:
        raise RuntimeError("forced 500 for CORS-header test")

    app.add_api_route(path, _boom, methods=["GET"])
    try:
        yield path
    finally:
        app.routes[:] = [r for r in app.routes if getattr(r, "path", None) != path]


def test_cors_header_on_actual_request(client: TestClient) -> None:
    resp = client.get("/api/v1/health", headers={"Origin": ALLOWED})
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ALLOWED


def test_cors_preflight_allows_patch(client: TestClient) -> None:
    """Preflight for the dialog's PATCH must pass (method + headers allowed)."""
    resp = client.options(
        "/api/v1/transactions/1",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ALLOWED


def test_cors_disallowed_origin_gets_no_allow_header(client: TestClient) -> None:
    """A non-allowlisted origin gets no allow-origin header (browser blocks)."""
    resp = client.get("/api/v1/health", headers={"Origin": "http://evil.example"})
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


def test_cors_header_present_on_500(client: TestClient, boom_route: str) -> None:
    """A genuine 500 must still carry the allow-origin header.

    Starlette's ServerErrorMiddleware builds the 500 outside CORSMiddleware, so
    without the app's catch-all handler the browser sees a missing-CORS error
    and misreports a real server error as "is the API running?" (the exact
    confusion that masked a stale-schema 500 during dev). raise_server_exceptions
    is off so we inspect the response the browser would receive.
    """
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get(boom_route, headers={"Origin": ALLOWED})
    assert resp.status_code == 500
    assert resp.headers["access-control-allow-origin"] == ALLOWED


def test_cors_500_disallowed_origin_gets_no_allow_header(
    client: TestClient, boom_route: str
) -> None:
    """The 500 handler echoes only allowlisted origins, never arbitrary ones."""
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get(boom_route, headers={"Origin": "http://evil.example"})
    assert resp.status_code == 500
    assert "access-control-allow-origin" not in resp.headers
