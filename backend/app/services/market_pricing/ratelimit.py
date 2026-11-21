"""Per-host pacing and a persistent circuit breaker.

Two separate mechanisms:

* **Pacing** (in memory): at most one request per host every
  ``min_interval + random jitter`` seconds. The worker is single-threaded per
  process, so concurrency per host is always 1.
* **Circuit breaker** (in the database, survives restarts): a 429/403 pauses the
  *whole host*, not just the job that hit it. Retrying job by job is exactly
  what turns a soft throttle into an IP ban. The pause doubles on every
  consecutive trip, honours ``Retry-After`` and resets on the first success.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.market_pricing import MarketHostState

from .config import HostPolicy
from .util import utcnow


class HostGate:
    def __init__(
        self,
        policies: Mapping[str, HostPolicy] | None = None,
        *,
        default: HostPolicy | None = None,
        clock: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._policies = dict(policies or {})
        self._default = default or HostPolicy()
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._rng = rng
        self._next_slot: dict[str, float] = {}
        self._lock = threading.Lock()

    def policy(self, host: str) -> HostPolicy:
        return self._policies.get(host, self._default)

    # -- pacing ---------------------------------------------------------------

    def wait_turn(self, host: str) -> float:
        """Block until this host may be called again. Returns seconds slept."""
        policy = self.policy(host)
        with self._lock:
            now = self._monotonic()
            slot = max(now, self._next_slot.get(host, now))
            gap = policy.min_interval_s + self._rng() * policy.jitter_s
            self._next_slot[host] = slot + gap
        wait = slot - now
        if wait > 0:
            self._sleep(wait)
        return max(0.0, wait)

    def seconds_until_slot(self, host: str) -> float:
        """How long ``wait_turn(host)`` would sleep right now."""
        with self._lock:
            now = self._monotonic()
            return max(0.0, self._next_slot.get(host, now) - now)

    # -- circuit breaker ------------------------------------------------------

    def paused_hosts(self, session: Session) -> dict[str, datetime]:
        now = self._clock()
        rows = session.scalars(
            select(MarketHostState).where(MarketHostState.paused_until.is_not(None))
        ).all()
        return {r.host: r.paused_until for r in rows if r.paused_until and r.paused_until > now}

    def paused_until(self, session: Session, host: str) -> datetime | None:
        return self.paused_hosts(session).get(host)

    def trip(
        self,
        session: Session,
        host: str,
        *,
        reason: str,
        retry_after_s: float | None = None,
        network: bool = False,
    ) -> datetime:
        """Pause ``host``. Returns the time it reopens."""
        policy = self.policy(host)
        state = session.get(MarketHostState, host)
        if state is None:
            state = MarketHostState(host=host, trips=0)
            session.add(state)
        base = policy.network_pause_s if network else policy.base_pause_s
        pause = min(policy.max_pause_s, base * (2 ** min(state.trips, 16)))
        if retry_after_s:
            pause = min(policy.max_pause_s, max(pause, retry_after_s))
        now = self._clock()
        state.trips += 1
        until = now + timedelta(seconds=pause)
        state.paused_until = until
        state.last_error = reason[:500]
        state.updated_at = now
        session.flush()
        return until

    def record_success(self, session: Session, host: str) -> None:
        state = session.get(MarketHostState, host)
        if state is not None and (state.trips or state.paused_until):
            state.trips = 0
            state.paused_until = None
            state.updated_at = self._clock()
