"""Transaction label helpers (PRD §F3a) — normalize, get-or-create, join writes.

(No caller list here: a hand-maintained one drifts by construction — the
previous version undercounted the callers and attributed the diffing
:func:`set_labels_on_transaction` to a module that actually uses the insert-only
:func:`link_labels`, which is exactly the distinction an editor of either
contract needs to get right. ``grep`` is authoritative. Same reasoning as the two
sibling modules :mod:`app.services.tag_service` and
:mod:`app.services.merchant_labels`.)

The two write primitives are NOT interchangeable — see their own docstrings for
which to reach for.

Distinct from :mod:`app.services.tag_service` (F3 merchant→*category* memory):
that learns categories; this owns freeform user labels.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db_errors import is_unique_violation, savepoint_insert
from app.models import Label, Transaction, TransactionLabel

_MAX_LABEL_LEN = 64
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_label_name(raw: str | None) -> str | None:
    """Canonicalize a user-typed label name, or ``None`` if it's empty.

    Rules: strip, drop one leading ``#`` (display-only — input is accepted with
    or without it), remove ``;`` (the backup-CSV delimiter — else a name
    containing ``;`` round-trips as two labels), collapse internal whitespace to
    single spaces, lowercase, cap at 64 chars. Empty after normalization →
    ``None``. Single source of truth for both the labels router and label
    assignment, so ``#Travel`` / ``travel`` / ``  travel  `` all resolve to one
    label.
    """
    if raw is None:
        return None
    s = raw.strip()
    if s.startswith("#"):
        s = s[1:]
    s = s.replace(";", "")
    s = _WHITESPACE_RE.sub(" ", s).strip().lower()
    if not s:
        return None
    return s[:_MAX_LABEL_LEN]


def _is_label_name_conflict(orig: BaseException | None) -> bool:
    """True when an ``IntegrityError.orig`` is the ``uq_labels_user_name`` violation.

    Delegates the dialect-aware matching to
    :func:`app.core.db_errors.is_unique_violation`.
    """
    return is_unique_violation(
        orig,
        index_name="uq_labels_user_name",
        columns=["labels.user_id", "labels.name"],
    )


def _get_or_create_label(session: Session, *, user_id: UUID, name: str) -> Label:
    """Return the existing ``(user_id, name)`` label or insert it.

    Race handling mirrors ``tag_service.record_tag`` via the shared
    :func:`app.core.db_errors.savepoint_insert`: the INSERT runs in a
    ``begin_nested()`` SAVEPOINT so a concurrent-insert conflict rolls back only
    the insert (caller-side pending state survives), then the winner is refetched.
    Unlike ``record_tag`` (heuristic learning that may degrade), a refetch miss
    here **raises** — this is an explicit user write and silently dropping the
    label is wrong.
    """
    label = Label(user_id=user_id, name=name)
    if savepoint_insert(session, label, is_conflict=_is_label_name_conflict):
        return label
    # Concurrent-insert race: the row now exists — refetch the winner.
    winner = session.scalar(select(Label).where(Label.user_id == user_id, Label.name == name))
    if winner is None:
        # The row that just tripped the unique constraint must be refetchable;
        # a miss means the label is being lost — surface it.
        raise RuntimeError("_get_or_create_label: winner refetch missed after unique conflict")
    return winner


def resolve_label_names(session: Session, *, user_id: UUID, names: Iterable[str]) -> list[Label]:
    """Return ``Label`` rows for ``names`` (normalized, order-preserving, deduped).

    Get-or-create: names without an existing label are inserted for this user.
    Blanks (empty after normalization) are dropped. Each missing label is created
    in its **own** SAVEPOINT (:func:`_get_or_create_label`) so one concurrent
    conflict never rolls back the non-conflicting siblings.
    """
    wanted: list[str] = []
    seen: set[str] = set()
    for raw in names:
        name = normalize_label_name(raw)
        if name is None or name in seen:
            continue
        seen.add(name)
        wanted.append(name)
    if not wanted:
        return []

    existing = {
        label.name: label
        for label in session.scalars(
            select(Label).where(Label.user_id == user_id, Label.name.in_(wanted))
        )
    }
    return [
        existing.get(name) or _get_or_create_label(session, user_id=user_id, name=name)
        for name in wanted
    ]


def set_labels_on_transaction(
    session: Session, *, txn: Transaction, labels: list[Label]
) -> set[int]:
    """Make ``txn``'s label set exactly ``labels`` — insert/delete join rows.

    Diffs against the **actual** ``transaction_labels`` rows (a fresh query), not
    the viewonly ``txn.labels`` collection (which can be stale after writes).
    Join rows carry ``user_id=txn.user_id`` (the composite same-user FK member the
    ``secondary`` relationship can't populate — the reason writes live here, not
    on the relationship). Expires ``txn.labels`` so the response reflects the
    saved set. Does not commit — the caller owns that.

    Returns the set of label ids newly inserted (``desired − current``) so a
    caller learning only the additions (PATCH) doesn't re-derive them from the
    stale viewonly collection.
    """
    desired = {label.id for label in labels}
    current = {
        tl.label_id: tl
        for tl in session.scalars(
            select(TransactionLabel).where(TransactionLabel.transaction_id == txn.id)
        )
    }
    for label_id, tl in current.items():
        if label_id not in desired:
            session.delete(tl)
    added: set[int] = set()
    for label in labels:
        if label.id not in current:
            session.add(
                TransactionLabel(
                    transaction_id=txn.id,
                    label_id=label.id,
                    user_id=txn.user_id,
                )
            )
            added.add(label.id)
    session.expire(txn, ["labels"])
    return added


def link_labels(session: Session, *, txn_id: int, user_id: UUID, label_ids: Iterable[int]) -> None:
    """Insert ``transaction_labels`` join rows for a fresh txn (no diff SELECT).

    Insert-only fast path for rows born with no labels (import prefill, backup
    restore): it assumes the txn has **no existing** ``(txn, label)`` links, so
    reuse on a non-fresh row would trip the join PK — use
    :func:`set_labels_on_transaction` for the diffing case. Dedups ``label_ids``
    (backup CSV can normalize two names — ``travel`` / ``#travel`` — to one id).
    Join rows carry ``user_id`` for the composite same-user FK. Caller commits.
    """
    for label_id in dict.fromkeys(label_ids):
        session.add(TransactionLabel(transaction_id=txn_id, label_id=label_id, user_id=user_id))
