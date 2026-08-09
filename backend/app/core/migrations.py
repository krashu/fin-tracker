"""Run Alembic migrations at application startup.

Local-first convenience: booting the app brings the connected database to
``head`` so a freshly pulled schema change (a new migration on ``main``)
applies on the next `make backend` instead of surfacing as a runtime 500 from
a stale table. Gated by :attr:`Settings.apply_migrations_on_startup` so the
hosted/v2 deployment — where migrations run as a discrete deploy step — and
the test suite — which builds its schema via ``Base.metadata.create_all`` —
can opt out.

The Alembic ``Config`` is built in-process **without** an ``.ini`` path on
purpose: ``env.py`` calls ``fileConfig()`` only when ``config_file_name`` is
set, and ``fileConfig`` would reconfigure stdlib logging out from under the
``configure_logging()`` call that runs first in the lifespan. Passing only
``script_location`` keeps env.py's ``fileConfig`` branch dormant; the database
URL is still sourced from :func:`app.core.config.get_settings` inside env.py,
so this upgrade targets the same DB as ``make migrate``.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from sqlalchemy.engine import make_url

from alembic import command
from app.core.config import Settings
from app.core.log_config import get_logger

logger = get_logger(__name__)

# migrations.py -> core/ -> app/ -> backend/. The `alembic/` script tree lives
# at the backend root next to alembic.ini.
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_LOCATION = _BACKEND_ROOT / "alembic"


def _ensure_sqlite_parent_dir(database_url: str) -> None:
    """Create the parent directory of a file-based SQLite DB if it's missing.

    A fresh checkout has no ``./data/`` dir, and SQLite can't create the DB file
    in a non-existent directory (``unable to open database file``). ``make
    migrate`` mkdir's it, but the app-boot path (``make backend``) does not — so
    do it here before ``command.upgrade`` connects. No-op for ``:memory:`` and
    non-SQLite URLs. Uses ``make_url`` rather than string-splitting so the sqlite
    URL variants (relative / absolute / query params) parse robustly.
    """
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite"):
        return
    # ``url.database`` is None for ``sqlite://`` (pure in-memory) and ":memory:"
    # for the explicit form — neither is a filesystem path to create.
    if not url.database or url.database == ":memory:":
        return
    Path(url.database).parent.mkdir(parents=True, exist_ok=True)


def run_migrations(settings: Settings) -> None:
    """Upgrade the configured database to ``head`` when enabled.

    A no-op (aside from a version-table read) once the DB is already current,
    so it is safe to run on every boot. Raises on migration failure — a DB the
    app can't bring to head is a refuse-to-boot condition, same posture as the
    ``V1_USER_ID`` guard in the lifespan.
    """
    if not settings.apply_migrations_on_startup:
        logger.info("migrations_startup_skipped", apply_migrations_on_startup=False)
        return

    _ensure_sqlite_parent_dir(settings.database_url)

    cfg = Config()
    cfg.set_main_option("script_location", str(_SCRIPT_LOCATION))

    logger.info("migrations_startup_begin", revision="head")
    command.upgrade(cfg, "head")
    logger.info("migrations_startup_applied", revision="head")
