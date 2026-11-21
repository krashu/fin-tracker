"""Shared DB-error classification + safe-insert helpers.

One place for the dialect-aware unique-constraint matching every router / service
previously hand-rolled (``accounts``, ``categories``, ``instruments``,
``transactions``, ``tag_service``, ``merchant_labels``, ``transaction_labels``).
Centralising the *algorithm* — not the per-constraint index/column data, which is
inherent — means a future hardening (a new dialect's error format, say) lands once.

:func:`savepoint_insert` centralises the ``begin_nested()`` insert-race dance the
rule-pin writes (``pin_tag`` / ``pin_label``) share.

:func:`insert_skip_existing` centralises the dialect-aware ``ON CONFLICT DO NOTHING``
bulk insert the external-source backfills (``fx_rates``, ``benchmark_nav``) each had
their own byte-identical copy of; :func:`insert_or_increment` is the single-statement
``ON CONFLICT DO UPDATE`` counter bump behind ``record_tag`` / ``record_label``
(ADR-0004 rule 7). Both build their statement through :func:`_dialect_insert`, the only
runtime ``if dialect == "sqlite" / elif "postgresql"`` fork in ``app/`` — precisely the
code a Postgres cutover must revisit (ADR-0001 rule 6).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from sqlalchemy.dialects import postgresql, sqlite
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

    Centralises the ``begin_nested()`` insert-race dance the rule pins repeat. The
    SAVEPOINT's ``__exit__`` flush surfaces the ``IntegrityError`` here (not at the
    outer commit); on the recognised conflict the savepoint rolls back ONLY the
    failed INSERT, leaving the parent transaction — and the caller's other pending
    state — intact.
    ``is_conflict`` is the per-constraint predicate (built on
    :func:`is_unique_violation`), so anything else — an FK failure, a future
    second constraint — re-raises rather than being silently swallowed.

    Used by ``pin_tag`` / ``pin_label``, which refetch-and-set ``pinned`` after a
    conflict (and RAISE on a refetch miss), deliberately not bumping ``hit_count``.
    The counter bumps (``record_tag`` / ``record_label``) are a single
    :func:`insert_or_increment` statement instead and never reach this helper.
    """
    try:
        with session.begin_nested():
            session.add(instance)
        return True
    except IntegrityError as e:
        if not is_conflict(e.orig):
            raise
        return False


def _dialect_insert(
    session: Session, model: type[DeclarativeBase], *, label: str
) -> sqlite.Insert | postgresql.Insert:
    """The dialect's ``insert(model)`` — the only runtime dialect fork in ``app/``.

    SQLAlchemy offers ``on_conflict_do_*`` only on the dialect-specific ``Insert``, so
    every upsert goes through here; a third dialect, or the Postgres cutover, lands
    once. ``label`` names the caller in the unsupported-dialect error.
    """
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        return sqlite.insert(model)
    if dialect == "postgresql":
        return postgresql.insert(model)
    raise RuntimeError(f"{label} upsert: unsupported dialect {dialect!r}")  # pragma: no cover


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
    """
    stmt = _dialect_insert(session, model, label=label).on_conflict_do_nothing(
        index_elements=conflict_cols
    )
    session.execute(stmt, rows)


def upsert_overwrite(
    session: Session,
    model: type[DeclarativeBase],
    rows: list[dict[str, object]],
    *,
    conflict_cols: list[str],
    update_cols: list[str],
    label: str,
) -> None:
    """Bulk ``INSERT … ON CONFLICT (conflict_cols) DO UPDATE SET col = EXCLUDED.col`` for ``model``.

    Updates specified columns when a conflict occurs on ``conflict_cols``.
    Portable across SQLite (v1) and PostgreSQL (v2).
    """
    if not rows:
        return
    stmt = _dialect_insert(session, model, label=label)
    set_clause = {col: getattr(stmt.excluded, col) for col in update_cols}
    upsert_stmt = stmt.on_conflict_do_update(
        index_elements=conflict_cols,
        set_=set_clause,
    )
    session.execute(upsert_stmt, rows)


def insert_or_increment[M: DeclarativeBase](
    session: Session,
    model: type[M],
    *,
    values: Mapping[str, object],
    conflict_cols: list[str],
    counter: str,
    touch: Mapping[str, object],
    label: str,
) -> M:
    """One ``INSERT … ON CONFLICT (conflict_cols) DO UPDATE SET counter = counter + 1 …
    RETURNING`` for ``model`` — an atomic counter bump with no read-modify-write window.

    ``values`` is the insert branch (so it carries the counter's initial value);
    ``touch`` is set on the conflict branch alongside the increment. ``touch`` must
    include ``updated_at``: the column's ``onupdate=`` does not fire for
    ``ON CONFLICT DO UPDATE``. ``populate_existing`` refreshes an instance already in
    the identity map, so the caller never reads a stale counter (``autoflush=False``
    sessions included). Python-side column defaults still apply on the insert branch.
    """
    stmt = (
        _dialect_insert(session, model, label=label)
        .values(**values)
        .on_conflict_do_update(
            index_elements=conflict_cols,
            set_={counter: getattr(model, counter) + 1, **touch},
        )
        .returning(model)
    )
    return session.scalars(stmt, execution_options={"populate_existing": True}).one()
