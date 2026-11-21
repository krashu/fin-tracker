"""SQLite housekeeping: WAL checkpoints, consistent backups, occasional VACUUM.

All functions are no-ops on PostgreSQL (use pg_dump and autovacuum there).
"""

from __future__ import annotations

import gzip
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine, text

from app.core.log_config import get_logger

from .util import utcnow

logger = get_logger(__name__)

try:  # Python 3.14+: Zstandard in the standard library
    from compression import zstd as _zstd
except ImportError:
    _zstd = None


def _is_sqlite(engine: Engine) -> bool:
    return engine.dialect.name == "sqlite"


def db_stats(engine: Engine) -> dict[str, float]:
    """Page statistics; ``free_ratio`` is the share of the file VACUUM would reclaim."""
    if not _is_sqlite(engine):
        return {}
    with engine.connect() as conn:
        page_size = conn.execute(text("PRAGMA page_size")).scalar_one()
        page_count = conn.execute(text("PRAGMA page_count")).scalar_one()
        freelist = conn.execute(text("PRAGMA freelist_count")).scalar_one()
    return {
        "page_size": page_size,
        "page_count": page_count,
        "freelist_count": freelist,
        "size_bytes": page_size * page_count,
        "free_ratio": (freelist / page_count) if page_count else 0.0,
    }


def checkpoint(engine: Engine) -> None:
    """Fold the WAL back into the main file and truncate it."""
    if not _is_sqlite(engine):
        return
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))


def vacuum_if_fragmented(engine: Engine, threshold: float = 0.20) -> bool:
    """VACUUM only when at least ``threshold`` of the file is free pages."""
    if not _is_sqlite(engine):
        return False
    stats = db_stats(engine)
    if stats.get("free_ratio", 0.0) < threshold:
        return False
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("VACUUM"))
    logger.info("sqlite_vacuum_completed", free_ratio=stats["free_ratio"])
    return True


def backup(
    engine: Engine,
    dest_dir: str | Path,
    *,
    keep: int = 7,
    now: datetime | None = None,
    prefix: str = "prices",
) -> Path | None:
    """Write a compressed, consistent snapshot and rotate old ones."""
    if not _is_sqlite(engine):
        return None
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    stamp = (now or utcnow()).strftime("%Y%m%dT%H%M%SZ")
    snapshot = dest / f"{prefix}-{stamp}.sqlite"
    if snapshot.exists():
        snapshot.unlink()

    checkpoint(engine)
    quoted = str(snapshot).replace("'", "''")
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f"VACUUM INTO '{quoted}'"))

    check = sqlite3.connect(snapshot)
    try:
        verdict = check.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        check.close()
    if verdict != "ok":
        snapshot.unlink(missing_ok=True)
        raise RuntimeError(f"backup snapshot failed integrity check: {verdict}")

    if _zstd is not None:
        archive = snapshot.with_name(snapshot.name + ".zst")
        with open(snapshot, "rb") as src, _zstd.open(archive, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
    else:
        archive = snapshot.with_name(snapshot.name + ".gz")
        with open(snapshot, "rb") as src, gzip.open(archive, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
    snapshot.unlink()

    archives = sorted(p for p in dest.glob(f"{prefix}-*.sqlite.*") if p.suffix in (".zst", ".gz"))
    for old in archives[: max(0, len(archives) - keep)]:
        old.unlink()
    logger.info("backup_written", archive=str(archive), size_bytes=archive.stat().st_size)
    return archive
