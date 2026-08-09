"""Root test fixtures shared across tests/parsers/, tests/api/, etc.

Pytest aggregates ``conftest.py`` files from rootdir down, so fixtures
defined here are visible to every subdir. Anything parser- or api-only
stays scoped to ``tests/parsers/conftest.py`` / ``tests/api/conftest.py``.

What lives here:

* :func:`_load_dotenv` — populates ``os.environ`` from the repo-root
  ``.env`` at import time. Runs once per pytest session because Python
  caches module imports; only fills keys absent from the live env, so
  shell exports always win.
* :data:`FIXTURES_ROOT` / :data:`LOCAL_ROOT` — anchor paths for test
  fixtures and the gitignored ``_local/`` real-PDF directory.
* Real-PDF + password fixtures (``axis_real_pdf``, ``axis_real_password``,
  ``icici_real_pdf``, ``icici_real_password``) — parametrize over the
  files matching ``_local/<issuer>_cc*.pdf`` and skip when absent / when
  the corresponding ``*_TEST_PWD`` env var is unset.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

FIXTURES_ROOT = Path(__file__).parent / "fixtures"
LOCAL_ROOT = FIXTURES_ROOT / "_local"


def _find_dotenv() -> Path | None:
    """Locate the repo-root ``.env``, anchored on the sibling ``.env.example``."""
    for parent in Path(__file__).resolve().parents:
        if (parent / ".env.example").is_file():
            env = parent / ".env"
            return env if env.is_file() else None
    return None


def _load_dotenv() -> None:
    """Populate ``os.environ`` from a ``.env`` file at the repo root, if present.

    Idempotent and forgiving: only fills keys absent from the live environment
    (so a shell ``$env:FOO`` or CI-injected value always wins). Recognises
    ``KEY=VALUE`` lines, optional leading ``export``, ``#`` comments, and
    single/double-quoted values. Values containing ``=`` or whitespace work
    via quoting; we don't expand ``$VAR`` references.
    """
    path = _find_dotenv()
    if path is None:
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_dotenv()

# Tests build their schema via ``Base.metadata.create_all`` against in-memory
# engines; the app lifespan (run by the ``client`` fixture) must NOT auto-migrate
# — that would run ``alembic upgrade head`` against the real file DB. Force off
# here (assignment, not setdefault) so a dev ``.env`` can't re-enable it.
os.environ["APPLY_MIGRATIONS_ON_STARTUP"] = "false"

# The lifespan (run by the ``client``/``unauth_client`` fixtures) would otherwise
# fire the demo seeder against the in-memory test DB — polluting the "empty
# tables" baseline every API test assumes. Force off here (assignment, not
# setdefault) so a dev ``.env`` can't re-enable it; the seeder gets its own
# targeted coverage in ``tests/services/test_demo_seed.py``.
os.environ["SEED_DEMO_ON_STARTUP"] = "false"


@pytest.fixture(scope="session", autouse=True)
def _configure_structlog_for_tests() -> None:
    """Idempotent with lifespan's call. Required for tests that construct
    ``TestClient(app)`` without ``with`` (e.g. ``test_health.py``) — those
    skip lifespan, so without this fixture structlog would run with default
    processors and the PII mask wouldn't be in the chain."""
    from app.core.log_config import configure_logging

    configure_logging()


def _glob_local(prefix: str) -> list[Path]:
    """Return sorted list of ``_local/<prefix>*.pdf`` paths (possibly empty)."""
    if not LOCAL_ROOT.exists():
        return []
    return sorted(LOCAL_ROOT.glob(f"{prefix}*.pdf"))


def _pdf_id(path: Path | None) -> str:
    if path is None:
        return "no-pdf"
    # Hash the resolved path so filenames containing card last-4, account
    # numbers, or statement dates never reach pytest cache, verbose output,
    # or pasted logs. Hash is machine-local (the resolved path includes the
    # user's absolute path); that's fine — IDs are pytest-local, not portable.
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
    return f"pdf-{digest[:8]}"


# Glob at module-load time so the parametrize() decorator below sees the
# list. Sentinel ``None`` is included when nothing is found so the tests
# still collect; the fixture body calls ``pytest.skip`` in that case.
_AXIS_PDFS: list[Path | None] = list(_glob_local("axis_cc")) or [None]  # type: ignore[list-item]
_ICICI_PDFS: list[Path | None] = list(_glob_local("icici_cc")) or [None]  # type: ignore[list-item]


@pytest.fixture(params=_AXIS_PDFS, ids=_pdf_id)
def axis_real_pdf(request: pytest.FixtureRequest) -> bytes:
    path: Path | None = request.param
    if path is None:
        pytest.skip(f"no Axis PDFs at {LOCAL_ROOT}/axis_cc*.pdf")
    return path.read_bytes()


@pytest.fixture
def axis_real_password() -> str:
    pwd = os.environ.get("AXIS_TEST_PWD")
    if not pwd:
        pytest.skip("AXIS_TEST_PWD env var not set")
    return pwd


@pytest.fixture(params=_ICICI_PDFS, ids=_pdf_id)
def icici_real_pdf(request: pytest.FixtureRequest) -> bytes:
    path: Path | None = request.param
    if path is None:
        pytest.skip(f"no ICICI PDFs at {LOCAL_ROOT}/icici_cc*.pdf")
    return path.read_bytes()


@pytest.fixture
def icici_real_password() -> str:
    pwd = os.environ.get("ICICI_TEST_PWD")
    if not pwd:
        pytest.skip("ICICI_TEST_PWD env var not set")
    return pwd
