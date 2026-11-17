"""One answer to "what time is it" (ADR-0001 rules 5b and 5c).

Three functions, and the rule for which one to call:

* :func:`naive_utcnow` — the current instant as naive UTC. **The only thing written to a
  ``DateTime`` column**, and the only shape Python compares against one (SQLite hands every
  column back naive). Aware is not portable: the columns are ``TIMESTAMP WITHOUT TIME ZONE``
  on Postgres, which assignment-casts an aware bind through the server's ``TimeZone`` and
  stores local wall-clock — SQLite silently drops the offset, so no SQLite test would see it.
  :func:`app.models.base.utcnow_default` writes ``created_at`` / ``updated_at`` / both
  ``last_used`` columns through it; ``confirmed_at``, ``archived_at`` and the ``sessions``
  arithmetic in :mod:`app.services.auth_service` call it directly. The gate is
  ``tests/services/test_datetime_boundary.py`` (and the aware-write guard every API test's
  route session carries); ``ty`` cannot help, since aware and naive are both ``datetime``.
* :func:`utcnow` — the same instant, timezone-AWARE. Only for values that never reach a
  column: a JWT claim, a filename, a JSON string.
* :func:`today` — the IST business date (:data:`BUSINESS_TZ`). Instants stay UTC; only the
  calendar is Indian.

``naive_utcnow`` and ``today`` both delegate to ``utcnow`` deliberately: patching
``clock.utcnow`` in a test moves the JWT clock, the session clock and the business date
TOGETHER.

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

from datetime import UTC, date, datetime, timedelta, timezone

# The business timezone, Asia/Kolkata, as a fixed +05:30 offset (ADR-0001 rule 5c). India has
# observed no DST since 1945, so the offset is exact for every date the app holds and needs no
# IANA database (``tzdata`` is absent from slim containers and Windows). A constant, not a
# setting: the app's calendar (business date, financial year) is Indian by definition.
BUSINESS_TZ = timezone(timedelta(hours=5, minutes=30), "IST")


def utcnow() -> datetime:
    """The current instant as timezone-aware UTC."""
    return datetime.now(UTC)


def today() -> date:
    """The business date: today in India (IST), whatever the host's timezone.

    Instants stay UTC; only the *date* is Indian. Between 18:30 and 24:00 UTC the UTC date
    is still yesterday in India, which would misdate NAV stamps, holdings ``as_of`` anchors
    and the financial-year boundary on 1 April. ``date.today()`` is no answer either: it
    reads the *host's* timezone, so the native (IST) and Docker (UTC) deployments disagreed
    on the same data. Every date fed to an ``as_of`` anchor comes from here.

    Delegates to :func:`utcnow`, so patching ``clock.utcnow`` freezes this too.
    """
    return utcnow().astimezone(BUSINESS_TZ).date()


def naive_utcnow() -> datetime:
    """The current instant as naive UTC — tzinfo stripped, never local time.

    What every ``DateTime`` column write and comparison uses (ADR-0001 rule 5b). Delegates
    to :func:`utcnow` so one patch moves both clocks.
    """
    return utcnow().replace(tzinfo=None)
