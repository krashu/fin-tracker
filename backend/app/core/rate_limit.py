"""Minimal in-process rate limiter for auth endpoints (PRD §Users & access v2).

A fixed-window counter keyed by ``(bucket, client-ip, window)``. In-process and
single-node — sufficient for a local-first / single-box deploy; a hosted
multi-worker setup would swap this for a shared store (Redis). Deliberately not a
new dependency (CLAUDE.md §2).

Used as a FastAPI dependency: ``Depends(RateLimit(bucket="login"))`` raises
429 once a client exceeds the window budget. Argon2id verification is expensive,
so throttling login/register/refresh guards both credential brute-force and CPU
exhaustion.
"""

from __future__ import annotations

import threading
import time

from fastapi import HTTPException, Request, status

from app.core.config import get_settings

# The one window length, shared by every bucket. Deliberately a constant and not
# a per-instance knob: the eviction sweep below is global over _store but the
# suffix it keeps is computed from the *calling* bucket's window, so two buckets
# on different windows would evict each other's live counters on every request.
# A per-bucket window therefore has to key eviction on (bucket, window) first.
_WINDOW_S = 60
# key -> hit count in the current window. Bounded churn: keys carry the window
# index, so stale windows are simply never read again (a few dozen live keys).
_store: dict[str, int] = {}
_last_sweep_window: int = 0
# Sync dependencies run in FastAPI's threadpool, so concurrent requests touch
# _store from different threads. Guard the evict + read-modify-write critical
# section: without it the counter can under-count (lost increments) and, under a
# free-threaded build, iterating _store while another thread inserts would raise.
_lock = threading.Lock()


def reset() -> None:
    """Clear all counters. Test hook (autouse fixture) — never called in prod."""
    global _last_sweep_window
    with _lock:
        _store.clear()
        _last_sweep_window = 0


def _client_ip(request: Request, *, trust_proxy: bool) -> str:
    """Resolve the client IP the limiter keys on.

    Direct mode (``trust_proxy`` off) uses the immediate peer. Behind the single-hop
    reverse-proxy overlay every peer is the proxy, so with ``trust_proxy`` on we key on the
    **rightmost** ``X-Forwarded-For`` entry — the address the trusted proxy itself observed.
    Rightmost is the exactly-one-trusted-hop choice and is spoof-resistant: the proxy appends
    the real peer, so a client-supplied XFF value is never rightmost (assumes a single trusted
    hop; a second untrusted proxy would break this). The value is only ever a bucket key, so a
    malformed entry is a harmless distinct bucket — no normalisation needed.
    """
    peer = request.client.host if request.client else "unknown"
    if not trust_proxy:
        return peer
    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return peer
    return forwarded.split(",")[-1].strip() or peer


class RateLimit:
    """Per-IP fixed-window limiter dependency for one logical bucket."""

    def __init__(self, *, bucket: str) -> None:
        self.bucket = bucket

    def __call__(self, request: Request) -> None:
        settings = get_settings()
        if not settings.rate_limit_enabled:
            return
        ip = _client_ip(request, trust_proxy=settings.rate_limit_trust_proxy)
        window = int(time.time() // _WINDOW_S)
        key = f"{self.bucket}:{ip}:{window}"
        suffix = f":{window}"
        with _lock:
            global _last_sweep_window
            # Evict keys from prior windows so a long-running process doesn't
            # accumulate one dead key per (bucket, ip) per window forever.
            # Sweeping the whole store is only safe because every bucket shares
            # _WINDOW_S — see the constant.
            if window != _last_sweep_window:
                for stale in [k for k in _store if not k.endswith(suffix)]:
                    _store.pop(stale, None)
                _last_sweep_window = window
            count = _store.get(key, 0) + 1
            _store[key] = count
        if count > settings.auth_rate_limit_per_minute:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="too many requests, slow down",
            )
