"""Shared DB-error classification + safe-insert helpers.

One place for the dialect-aware unique-constraint matching every router / service
previously hand-rolled (``accounts``, ``categories``, ``instruments``,
``transactions``, ``tag_service``, ``merchant_labels``, ``transaction_labels``).
Centralising the *algorithm* — not the per-constraint index/column data, which is
inherent — means a future hardening (a new dialect's error format, say) lands once.

:func:`savepoint_insert` centralises the ``begin_nested()`` insert-race dance the
merchant-map / label upserts each hand-rolled, so the Postgres v2 ``ON CONFLICT``
cutover (and any flush-timing fix) is a single edit.

:func:`insert_skip_existing` centralises the dialect-aware ``ON CONFLICT DO NOTHING``
bulk insert the external-source backfills (``fx_rates``, ``benchmark_nav``) each had
their own byte-identical copy of. It holds the only runtime
``if dialect == "sqlite" / elif "postgresql"`` fork in ``app/``, which is precisely
the code a Postgres cutover must revisit — see ADR-0001.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import Insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Session

_SQLITE_UNIQUE_MARKER = "unique constraint failed:"


def is_unique_violation(orig: BaseException | None, *, index_name: str, columns: list[str]) -> bool:
    """True when a DB error is the named unique-constraint / index violation.

    SQLite reports the failing *columns* (``UNIQUE constraint failed: t.a, t.b``);
    Postgres reports the *index name*. Match both so the 409 mapping works on either
    dialect. ``columns`` are ``"table.col"`` tokens (all must be present).

    The SQLite branch matches on **whole ``table.col`` tokens**, not substrings, so
    a constraint whose column set is a substring of another's message can't be
    misclassified (e.g. ``labels.user_id`` must NOT match a
    ``transaction_labels.user_id`` conflict). The Postgres branch keeps the
    index-name check (its message names the index, never the columns).
    """
    err = str(orig).lower()
    if index_name.lower() in err:
        return True
    idx = err.find(_SQLITE_UNIQUE_MARKER)
    if idx == -1:
        return False
    # SQLite lists the failing columns after the marker as ", "-separated
    # ``table.col`` tokens — compare exact tokens, not substrings.
    tokens = {t.strip() for t in err[idx + len(_SQLITE_UNIQUE_MARKER) :].split(",") if t.strip()}
    return bool(columns) and all(col.lower() in tokens for col in columns)


def savepoint_insert(
    session: Session,
    instance: object,
    *,
    is_conflict: Callable[[BaseException | None], bool],
) -> bool:
    """INSERT ``instance`` inside a SAVEPOINT. Return ``True`` when it committed,
    ``False`` on a recognised unique-conflict (the caller then refetches the
    winner). Re-raise any other ``IntegrityError``.

    Centralises the ``begin_nested()`` insert-race dance the merchant→category /
    merchant→label upserts each repeated. The SAVEPOINT's ``__exit__`` flush
    surfaces the ``IntegrityError`` here (not at the outer commit); on the
    recognised conflict the savepoint rolls back ONLY the failed INSERT, leaving
    the parent transaction — and the caller's other pending state — intact.
    ``is_conflict`` is the per-constraint predicate (built on
    :func:`is_unique_violation`), so anything else — an FK failure, a future
    second constraint — re-raises rather than being silently swallowed.

    Postgres v2 is **not** one edit here, despite what this docstring used to claim.
    Scoping it honestly: (a) a native ``INSERT ... ON CONFLICT DO UPDATE`` needs the
    SET expression passed in, which this ``(session, instance, is_conflict) -> bool``
    signature has no channel for — the increment is per-model (``hit_count += 1``,
    ``last_used``); (b) two of the callers want different post-conflict semantics
    anyway — ``pin_tag`` / ``pin_label`` refetch-and-set ``pinned`` and RAISE on a
    refetch miss, deliberately not bumping ``hit_count``, while ``record_*`` logs and
    returns. One edit inside this function cannot produce all three behaviours.
    (c) The cutover also spans :func:`insert_skip_existing` below and
    ``import_service``'s occurrence loop. See ADR-0001.
    """
    try:
        with session.begin_nested():
            session.add(instance)
        return True
    except IntegrityError as e:
        if not is_conflict(e.orig):
            raise
        return False


def insert_skip_existing(
    session: Session,
    model: type[DeclarativeBase],
    rows: list[dict[str, object]],
    *,
    conflict_cols: list[str],
    label: str,
) -> None:
    """Bulk ``INSERT … ON CONFLICT (conflict_cols) DO NOTHING`` for ``model``.

    The external-source backfills (``fx_rates``, ``benchmark_nav``) share this: each
    is keyed on a natural key with no per-row user decision, so a concurrent refresh
    should skip rather than crash on the unique index. ``label`` names the table in
    the unsupported-dialect error.

    Portable across the roadmapped SQLite (v1) → Postgres (v2) move. The mapped
    column's bind processor applies to the executemany, so scaled types (``FxRate``
    on ``rate``, ``PriceNative`` on ``nav``) still convert on the way in.

    This is the only runtime dialect fork in ``app/``; a third dialect, a switch to
    ``DO UPDATE``, or any fix to the bind-processor assumption above lands here once
    rather than in each caller.
    """
    dialect = session.get_bind().dialect.name
    stmt: Insert
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = sqlite_insert(model).on_conflict_do_nothing(index_elements=conflict_cols)
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(model).on_conflict_do_nothing(index_elements=conflict_cols)
    else:  # pragma: no cover - only SQLite (v1) / Postgres (v2) are supported
        raise RuntimeError(f"{label} upsert: unsupported dialect {dialect!r}")
    session.execute(stmt, rows)
