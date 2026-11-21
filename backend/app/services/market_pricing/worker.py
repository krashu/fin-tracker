"""The one place that talks to upstream APIs.

A single synchronous loop: claim the best runnable job (preferring a host whose
pacing slot is already free), wait for that slot, fetch, persist. Run it in a daemon thread.

Failure handling
----------------
429 / 403        pause the whole host (circuit breaker), re-queue, attempt not counted
network down     short host pause, re-queue, attempt not counted
5xx / bad body   per-job exponential backoff, fails after ``max_job_attempts``
unknown symbol   job fails; instrument marked not_found (or dead if it had data)
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.orm import Session, sessionmaker

from app.core.log_config import get_logger
from app.models.market_pricing import JOB_REFRESH, MarketInstrument

from . import jobs, store
from .config import Settings
from .providers.amfi import AmfiBulkClient
from .providers.base import (
    FetchResult,
    InstrumentNotFound,
    PriceProvider,
    ProviderError,
    RateLimited,
    TransientUpstreamError,
)
from .ratelimit import HostGate
from .util import utcnow

logger = get_logger(__name__)


class _ResolverUnavailable(Exception):
    """The ISIN -> scheme-code lookup cannot run right now; try the job again later."""

    def __init__(self, until: datetime, message: str) -> None:
        super().__init__(message)
        self.until = until


@dataclass(frozen=True, slots=True)
class _Claim:
    job_id: int
    instrument_id: int
    price_key: str
    asset_class: str
    host: str
    kind: str
    range_start: date | None
    provider_symbol: str | None
    attempts: int


class PriceWorker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        providers: Mapping[str, PriceProvider],
        gate: HostGate,
        *,
        amfi: AmfiBulkClient | None = None,
        settings: Settings | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessions = session_factory
        self._providers = dict(providers)
        self._gate = gate
        self._amfi = amfi
        self._settings = settings or Settings()
        self._clock = clock
        self._wake = threading.Event()

    # -- loop -----------------------------------------------------------------

    def run_forever(self, stop: threading.Event, idle_sleep_s: float = 5.0) -> None:
        with self._sessions.begin() as session:
            recovered = jobs.recover_interrupted(session)
        if recovered:
            logger.info("re_queued_interrupted_jobs", count=recovered)
        while not stop.is_set():
            try:
                worked = self.run_once()
            except Exception as exc:  # never let the loop die
                logger.error("worker_iteration_failed", error=str(exc))
                worked = False
            if not worked and not stop.is_set():
                self._wake.wait(idle_sleep_s)
                self._wake.clear()

    def notify(self) -> None:
        """Wake an idle worker now (call after queueing work or to stop it)."""
        self._wake.set()

    def drain(self, max_jobs: int | None = None) -> int:
        """Process jobs until none is runnable right now. Returns jobs handled."""
        done = 0
        while (max_jobs is None or done < max_jobs) and self.run_once():
            done += 1
        return done

    def run_once(self) -> bool:
        """Handle one job. False if nothing is runnable (empty, delayed or host paused)."""
        claim = self._claim()
        if claim is None:
            return False
        self._process(claim)
        return True

    # -- steps ----------------------------------------------------------------

    def _claim(self) -> _Claim | None:
        with self._sessions.begin() as session:
            now = self._clock()
            paused = self._gate.paused_hosts(session)
            heads = jobs.runnable_heads(session, now=now, paused_hosts=paused.keys())
            if not heads:
                return None
            job = min(
                heads,
                key=lambda j: (self._gate.seconds_until_slot(j.host), -j.priority, j.id),
            )
            jobs.mark_running(job, now)
            instrument = session.get(MarketInstrument, job.instrument_id)
            if instrument is None:  # evicted while queued
                session.delete(job)
                return None
            return _Claim(
                job_id=job.id,
                instrument_id=instrument.id,
                price_key=instrument.price_key,
                asset_class=instrument.asset_class,
                host=job.host,
                kind=job.kind,
                range_start=job.range_start,
                provider_symbol=instrument.provider_symbol,
                attempts=job.attempts,
            )

    def _process(self, claim: _Claim) -> None:
        provider = self._providers.get(claim.host)
        if provider is None:
            self._fail(claim, f"no provider registered for host {claim.host!r}", not_found=False)
            return
        start = claim.range_start if claim.kind == JOB_REFRESH else None
        try:
            symbol = claim.provider_symbol or self._resolve_symbol(claim)
            self._gate.wait_turn(claim.host)
            result = provider.fetch_history(symbol, start)
        except _ResolverUnavailable as exc:
            with self._sessions.begin() as session:
                jobs.release(
                    session, claim.job_id, not_before=exc.until, error=str(exc), count_attempt=False
                )
        except RateLimited as exc:
            self._on_blocked(claim, exc)
        except InstrumentNotFound as exc:
            self._fail(claim, str(exc), not_found=True)
        except TransientUpstreamError as exc:
            if exc.network:
                self._on_network_down(claim, exc)
            else:
                self._on_transient(claim, str(exc))
        except ProviderError as exc:
            self._on_transient(claim, str(exc))
        except Exception as exc:  # parser bug, unexpected payload...
            logger.error("unexpected_failure_fetching", price_key=claim.price_key, error=str(exc))
            self._on_transient(claim, f"{type(exc).__name__}: {exc}")
        else:
            self._on_success(claim, symbol, result, full_history=start is None)

    def _resolve_symbol(self, claim: _Claim) -> str:
        """Mutual fund ISIN -> AMFI scheme code, via the cached NAVAll file."""
        if claim.asset_class != "mf" or self._amfi is None:
            raise InstrumentNotFound(f"{claim.price_key}: no provider symbol and no resolver")
        with self._sessions() as session:
            paused_until = self._gate.paused_until(session, "amfi")
        if paused_until is not None:
            raise _ResolverUnavailable(paused_until, "AMFI directory is paused")
        try:
            code = self._amfi.scheme_code_for(claim.price_key)
        except (RateLimited, TransientUpstreamError) as exc:
            blocked = isinstance(exc, RateLimited)
            if not blocked and not exc.network:
                raise
            with self._sessions.begin() as session:
                until = self._gate.trip(
                    session,
                    "amfi",
                    reason=str(exc),
                    retry_after_s=exc.retry_after_s if blocked else None,
                    network=not blocked,
                )
            raise _ResolverUnavailable(until, f"AMFI directory unavailable: {exc}") from exc
        if code is None:
            raise InstrumentNotFound(f"{claim.price_key}: ISIN not present in AMFI NAVAll")
        return code

    # -- outcomes -------------------------------------------------------------

    def _on_success(
        self, claim: _Claim, symbol: str, result: FetchResult, *, full_history: bool
    ) -> None:
        now = self._clock()
        with self._sessions.begin() as session:
            instrument = session.get(MarketInstrument, claim.instrument_id)
            if instrument is None:
                jobs.complete(session, claim.job_id)
                return
            if not instrument.provider_symbol:
                instrument.provider_symbol = symbol
            written = store.apply_fetch_result(
                session, instrument, result, full_history=full_history, now=now
            )
            jobs.complete(session, claim.job_id)
            self._gate.record_success(session, claim.host)
        logger.info(
            "market_price_fetch_success",
            kind=claim.kind,
            price_key=claim.price_key,
            points_fetched=len(result.points),
            rows_written=written,
        )

    def _on_blocked(self, claim: _Claim, exc: RateLimited) -> None:
        with self._sessions.begin() as session:
            until = self._gate.trip(
                session, claim.host, reason=str(exc), retry_after_s=exc.retry_after_s
            )
            jobs.release(
                session, claim.job_id, not_before=until, error=str(exc), count_attempt=False
            )
        logger.warning("host_rate_limited", host=claim.host, paused_until=str(until))

    def _on_network_down(self, claim: _Claim, exc: TransientUpstreamError) -> None:
        with self._sessions.begin() as session:
            until = self._gate.trip(session, claim.host, reason=str(exc), network=True)
            jobs.release(
                session, claim.job_id, not_before=until, error=str(exc), count_attempt=False
            )
        logger.warning("network_down_for_host", host=claim.host, paused_until=str(until))

    def _on_transient(self, claim: _Claim, error: str) -> None:
        attempts = claim.attempts + 1
        if attempts >= self._settings.max_job_attempts:
            self._fail(claim, error, not_found=False)
            return
        delay = jobs.backoff_delay(
            claim.attempts, self._settings.retry_base_s, self._settings.retry_max_s
        )
        with self._sessions.begin() as session:
            jobs.release(
                session,
                claim.job_id,
                not_before=self._clock() + delay,
                error=error,
                count_attempt=True,
            )
            instrument = session.get(MarketInstrument, claim.instrument_id)
            if instrument is not None:
                store.mark_error(session, instrument, error, now=self._clock())
        logger.warning(
            "market_price_fetch_retry",
            price_key=claim.price_key,
            attempt=attempts,
            error=error,
        )

    def _fail(self, claim: _Claim, error: str, *, not_found: bool) -> None:
        with self._sessions.begin() as session:
            jobs.fail(session, claim.job_id, error)
            instrument = session.get(MarketInstrument, claim.instrument_id)
            if instrument is not None:
                if not_found:
                    store.mark_not_found(session, instrument, error, now=self._clock())
                    self._gate.record_success(session, claim.host)
                else:
                    store.mark_error(session, instrument, error, now=self._clock())
        logger.error("market_price_fetch_failed", price_key=claim.price_key, error=error)


def start_worker_thread(worker: PriceWorker) -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()
    thread = threading.Thread(
        target=worker.run_forever, args=(stop,), name="fin-pricing-worker", daemon=True
    )
    thread.start()
    return thread, stop


__all__ = ["PriceWorker", "start_worker_thread"]
