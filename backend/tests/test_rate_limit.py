"""Auth rate-limiter client-IP resolution (PRD §Users & access v2).

Covers the opt-in trusted-proxy X-Forwarded-For key added in the Phase-4 session-
hardening pass. Direct mode keys on the immediate peer; trust-proxy mode keys on the
**rightmost** XFF entry — the exactly-one-trusted-hop, spoof-resistant choice (the
proxy appends the real peer, so a client-supplied value is never rightmost). Malformed
/ missing headers fall back to the peer. The extracted value is only ever a bucket key,
so no normalisation is attempted.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import rate_limit
from app.core.rate_limit import RateLimit, _client_ip


def _req(peer: str | None, xff: str | None) -> SimpleNamespace:
    headers = {"x-forwarded-for": xff} if xff is not None else {}
    client = SimpleNamespace(host=peer) if peer is not None else None
    return SimpleNamespace(client=client, headers=headers)


def test_direct_mode_keys_on_peer_and_ignores_xff() -> None:
    assert _client_ip(_req("10.0.0.1", "9.9.9.9"), trust_proxy=False) == "10.0.0.1"


def test_trust_proxy_uses_single_xff_entry() -> None:
    assert _client_ip(_req("10.0.0.1", "203.0.113.5"), trust_proxy=True) == "203.0.113.5"


def test_trust_proxy_takes_rightmost_entry_on_multi_hop() -> None:
    # A client-spoofed leading value is defeated: the trusted proxy appends the real
    # peer, so rightmost is the address the proxy actually observed.
    assert _client_ip(_req("10.0.0.1", "1.2.3.4, 203.0.113.9"), trust_proxy=True) == "203.0.113.9"


def test_trust_proxy_falls_back_to_peer_when_header_absent() -> None:
    assert _client_ip(_req("10.0.0.1", None), trust_proxy=True) == "10.0.0.1"


def test_trust_proxy_falls_back_to_peer_on_blank_xff() -> None:
    assert _client_ip(_req("10.0.0.1", "   "), trust_proxy=True) == "10.0.0.1"


def test_no_client_and_no_header_is_unknown() -> None:
    assert _client_ip(_req(None, None), trust_proxy=True) == "unknown"


def test_trust_proxy_buckets_distinct_xff_ips_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two clients behind one proxy get independent buckets — one hammering login
    doesn't throttle the other (the shared-bucket bug the flag exists to fix)."""
    settings = SimpleNamespace(
        rate_limit_enabled=True, rate_limit_trust_proxy=True, auth_rate_limit_per_minute=2
    )
    monkeypatch.setattr(rate_limit, "get_settings", lambda: settings)
    monkeypatch.setattr(rate_limit.time, "time", lambda: 1_000_000.0)
    rate_limit.reset()
    limiter = RateLimit(bucket="login")

    # IP A exhausts its 2-request budget; the third is throttled.
    for _ in range(2):
        limiter(_req("10.0.0.1", "1.1.1.1"))
    with pytest.raises(HTTPException) as exc:
        limiter(_req("10.0.0.1", "1.1.1.1"))
    assert exc.value.status_code == 429

    # A different XFF client (same peer) is still allowed — separate bucket.
    limiter(_req("10.0.0.1", "2.2.2.2"))
    rate_limit.reset()


def test_a_prior_windows_key_is_evicted_when_the_clock_crosses_a_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stale-key sweep fires, and the budget restarts in the new window.

    Every other test in this module freezes ``time.time()`` at a single value, so
    no key is ever stale and ``_store.pop`` is never reached — this is the only
    test that crosses a window boundary.

    What it pins is the invariant the whole store depends on: the sweep is global
    over ``_store`` but keeps only the *calling* bucket's suffix, so it is correct
    exactly as long as every bucket shares ``_WINDOW_S``. Re-introduce a per-bucket
    window and this test still passes at the default — the guard is the constant,
    not this assertion.
    """
    settings = SimpleNamespace(
        rate_limit_enabled=True, rate_limit_trust_proxy=False, auth_rate_limit_per_minute=2
    )
    monkeypatch.setattr(rate_limit, "get_settings", lambda: settings)
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(rate_limit.time, "time", lambda: clock["t"])
    # tests/api/conftest.py's autouse reset does not reach this module.
    rate_limit.reset()
    limiter = RateLimit(bucket="login")

    # Exhaust the budget inside window N.
    for _ in range(2):
        limiter(_req("10.0.0.1", None))
    with pytest.raises(HTTPException) as exc:
        limiter(_req("10.0.0.1", None))
    assert exc.value.status_code == 429
    stale_key = f"login:10.0.0.1:{int(clock['t'] // rate_limit._WINDOW_S)}"
    assert rate_limit._store == {stale_key: 3}

    # Cross into window N+1: the dead key is swept and the budget starts over.
    clock["t"] += rate_limit._WINDOW_S
    limiter(_req("10.0.0.1", None))
    fresh_key = f"login:10.0.0.1:{int(clock['t'] // rate_limit._WINDOW_S)}"
    assert rate_limit._store == {fresh_key: 1}

    rate_limit.reset()
