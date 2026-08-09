"""Server entrypoint: ``uv run main.py`` starts the API.

The FastAPI app itself lives in :mod:`app.main`; this module only wraps the
uvicorn launch and sources its bind config (host / port / reload) from
:class:`app.core.config.Settings`, i.e. the repo-root ``.env``. Production-safe
defaults (``127.0.0.1`` / ``8000`` / reload off) apply when ``.env`` is silent;
set ``API_HOST`` / ``API_PORT`` / ``API_RELOAD`` there to override. Run it from the
``backend/`` directory so uvicorn's reloader can import the ``app`` package.
"""

from __future__ import annotations

import uvicorn

from app.core.config import get_settings

if __name__ == "__main__":
    settings = get_settings()
    # Import string (not the app object) so the --reload subprocess can
    # re-import on change.
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
    )
