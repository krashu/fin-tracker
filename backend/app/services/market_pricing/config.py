"""Runtime settings for market pricing engine."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)


@dataclass(frozen=True, slots=True)
class HostPolicy:
    """Pacing and circuit-breaker policy for one upstream host."""

    min_interval_s: float = 2.0  # minimum gap between two requests
    jitter_s: float = 1.0  # extra random gap, 0..jitter_s
    base_pause_s: float = 900.0  # first pause after a 429/403 (15 min)
    max_pause_s: float = 6 * 3600.0  # pause ceiling when blocks repeat
    network_pause_s: float = 300.0  # first pause when the network is down


def default_host_policies() -> dict[str, HostPolicy]:
    return {
        "mfapi": HostPolicy(min_interval_s=1.5, jitter_s=1.0),
        "yahoo": HostPolicy(min_interval_s=2.5, jitter_s=1.5),
        # One request a day in normal operation; the gap only guards retries.
        "amfi": HostPolicy(min_interval_s=30.0, jitter_s=0.0),
    }


@dataclass(slots=True)
class Settings:
    database_url: str = "sqlite:///fin_tracker_prices.db"

    # HTTP
    user_agent: str = DEFAULT_USER_AGENT
    http_timeout_s: float = 20.0
    host_policies: dict[str, HostPolicy] = field(default_factory=default_host_policies)

    # Upstreams
    mfapi_base_url: str = "https://api.mfapi.in"
    yahoo_base_url: str = "https://query1.finance.yahoo.com"
    amfi_urls: tuple[str, ...] = (
        "https://portal.amfiindia.com/spages/NAVAll.txt",
        "https://www.amfiindia.com/spages/NAVAll.txt",
    )
    amfi_cache_ttl_s: float = 6 * 3600.0
    yahoo_close_is_split_adjusted: bool = True

    # Job queue
    max_job_attempts: int = 5
    retry_base_s: float = 60.0
    retry_max_s: float = 3600.0

    # Daily refresh
    refresh_overlap_days: int = 5  # re-read window, picks up late corrections
    min_refresh_interval_h: float = 12.0  # never refresh one ticker more often

    # Retention
    gc_grace_days: int = 90
    vacuum_freelist_threshold: float = 0.20

    # In-process scheduler (hours between runs)
    refresh_interval_h: float = 24.0
    gc_interval_h: float = 168.0
    backup_interval_h: float = 24.0
    backup_dir: str | None = None
    backup_keep: int = 7

    # SQL returning (price_key, first_trade_date, last_activity_date, is_open)
    usage_sql: str | None = None

    @classmethod
    def from_env(cls, prefix: str = "FINPRICING_") -> Settings:
        """Build settings from ``FINPRICING_*`` environment variables."""
        settings = cls()
        for f in fields(cls):
            raw = os.environ.get(prefix + f.name.upper())
            if raw is None or f.name == "host_policies":
                continue
            current = getattr(settings, f.name)
            if isinstance(current, bool):
                value: object = raw.strip().lower() in {"1", "true", "yes", "on"}
            elif isinstance(current, int):
                value = int(raw)
            elif isinstance(current, float):
                value = float(raw)
            elif isinstance(current, tuple):
                value = tuple(p.strip() for p in raw.split(",") if p.strip())
            else:
                value = raw
            setattr(settings, f.name, value)
        return settings
