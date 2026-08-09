"""One answer to "what time is it".

Before this module there were five coexisting implementations of the current instant, and the
naive-UTC rule that ties them together lived only as prose in three docstrings — so every write
site re-derived it and ``ty`` could not see the difference (aware and naive are both
``datetime``). The concrete breakage that follows from that: ``confirmed_at`` is WRITTEN aware
(``api/v1/transactions.py``, ``api/v1/imports.py``) and READ BACK NAIVE from SQLite, so any
sub-day comparison written as ``datetime.now(UTC) - txn.confirmed_at`` raises ``TypeError`` on
real data while passing every test that never re-reads the column.

Two functions, because the DB genuinely holds both shapes today:

* :func:`utcnow` — timezone-AWARE UTC. SQLite's ``DATETIME`` bind processor formats the fields
  and drops the tzinfo, so on SQLite what lands on disk is naive UTC either way. **That is a
  SQLite-only guarantee**: the same columns are ``TIMESTAMP WITHOUT TIME ZONE`` on Postgres,
  which assignment-casts an aware bind through the server's ``TimeZone`` setting and stores
  local wall-clock. So aware is safe only for a column nothing does Python arithmetic on.
* :func:`naive_utcnow` — the same instant with tzinfo stripped, and therefore
  dialect-independent. Used by the ``sessions`` table's hand-written arithmetic in
  :mod:`app.services.auth_service` and by :func:`app.models.base.utcnow_default`, which writes
  ``created_at`` / ``updated_at`` / both ``last_used`` columns. Prefer it for anything Python
  reads back and computes on.

``naive_utcnow`` delegates to ``utcnow`` deliberately: patching ``clock.utcnow`` in a test moves
the JWT clock and the session clock TOGETHER. That was impossible before — the two seams were
private symbols in different modules with opposite tz conventions, so patching the one that
advertised itself as the seam proved nothing about ``rotate_session``.

It is also expected to be TRANSITIONAL. Remediation steps 11-13 move the DB-side arithmetic and
settle the naive/aware convention; ``naive_utcnow`` should not be read as the settled answer.

Import the MODULE, not the functions — ``from app.core import clock`` then ``clock.utcnow()``.
A ``from``-import binds the name into each consumer's namespace, which would force a test to
patch every module separately; this way ``clock.utcnow`` is one patch target for all call sites.

DELIBERATELY NOT OWNED HERE, so the "one answer" claim stays honest:

* ``core/rate_limit.py``'s ``time.time()`` — a float epoch for ``// _WINDOW_S`` bucketing, not an
  instant, and it already has its own established monkeypatch seam.
* ``middleware.py``'s ``perf_counter()`` — elapsed duration, a different question entirely.
* DB-side ``func.now()`` on ``TimestampMixin`` and both merchant maps — still declared, but only
  as the backstop for raw ``INSERT``s that bypass the ORM. Every ORM write now comes from
  :func:`app.models.base.utcnow_default`. (Kept in the models because
  ``tests/test_migration_parity`` compares DB-side defaults.)
* the ``as_of`` FX read inside :func:`app.services.portfolio_service.compute_portfolio_summary`
  — ``rate_on(on=as_of)`` is deliberately as-of anchored so a backfill values a *past*
  portfolio at that date's rate. It takes whatever ``as_of`` the caller supplies; the routes
  supply :func:`today`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime


def utcnow() -> datetime:
    """The current instant as timezone-aware UTC."""
    return datetime.now(UTC)


def today() -> date:
    """Today's date in UTC — never the server's local date.

    ``date.today()`` reads the *host's* timezone, so the native (IST) and Docker (UTC)
    deployments reported different staleness numbers for identical data at the same instant.
    Every date fed to an ``as_of`` anchor comes from here so the two agree.

    Delegates to :func:`utcnow`, so patching ``clock.utcnow`` freezes this too.
    """
    return utcnow().date()


def naive_utcnow() -> datetime:
    """The current instant as naive UTC — tzinfo stripped, never local time.

    Only for hand-written comparisons against columns SQLite hands back naive. Delegates to
    :func:`utcnow` so one patch moves both clocks.
    """
    return utcnow().replace(tzinfo=None)
