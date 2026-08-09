"""``/api/v1/imports`` — upload a statement (PRD §F1) + review/commit lifecycle.

* ``POST /imports`` — upload a statement file. Thin route: validates the
  upload size, delegates to :func:`app.services.import_service.import_statement`,
  commits, and returns the summary. Exception → HTTP mapping is hardcoded
  with **generic** detail strings — no ``account_id``, password, issuer, or
  exception args echoed — so a future Sentry / log-capture path doesn't
  surface user-supplied input through the response body either.

  Mapping:
  * :class:`AccountNotFoundError` → 404 ``"account not found"``
  * :class:`NonInrAccountError` → 422 ``"statement import requires an INR account"``
  * :class:`ParserNotRegisteredError` → 422 ``"no parser registered for this issuer/type"``
  * :class:`InvalidPasswordError` → 422 ``"incorrect or missing PDF password"``
  * :class:`ParserError` → 422 ``"could not parse statement file"``

  ``InvalidPasswordError`` is-a ``ParserError`` so the specific catch must
  come first; the inline comment guards a future accidental reorder.

  Re-upload of an already-imported file reconciles rather than short-circuits
  (see :func:`import_statement`): it re-parses — so a protected PDF needs its
  password again — and returns ``pending_count`` (the batch's rows still
  awaiting review) for the frontend to route on.

* ``POST /imports/investments`` — upload a canonical investment-transaction CSV
  (PRD §F7). Account-less; commits directly (no review queue — investments have
  no categories) and returns a summary with PII-safe per-row warnings. Generic
  error mapping (``ParserError`` → 422), same as ``POST /imports``.

* ``GET /imports/pending`` — open batches (≥1 unconfirmed row) with a
  per-batch ``pending_count`` + account label, newest first. Feeds the
  notification-bell dropdown and gives a way back to a review queue after
  navigating away (there is no other batch-list endpoint).

* ``GET /imports/{batch_id}/candidates`` — pending rows of this batch with
  ``prior_matches`` (LEFT-joined ``merchant_tag_map.hit_count``) +
  ``confidence`` (computed read-time from thresholds). The frontend's review
  queue surface. Why not ``GET /transactions?import_batch_id=X&status=pending``:
  candidates carry ``prior_matches`` / ``confidence`` which ``TransactionRead``
  deliberately omits per its docstring.

* ``POST /imports/{batch_id}/commit`` — bulk-stamp ``confirmed_at`` on the
  listed ids. Atomic: any invalid id → 422 with ``invalid_ids: [...]``, no
  writes. Body is id-list only — category edits the user made in the queue
  must be PATCHed to ``/transactions/{id}`` before commit. Commit reads
  ``category_id`` from the DB, not the request.

* ``DELETE /imports/{batch_id}`` — cancel: hard-delete pending rows of this
  batch. If zero rows remain on the batch after delete, hard-delete the
  ``ImportBatch`` row too so re-upload re-runs the parser. Partial cancel
  (any confirmed rows survive) keeps the batch row; a re-upload of the same
  file then re-parses and reconciles against the DB, so the cancelled rows
  **re-surface as pending** on that same batch. See
  :func:`cancel_import_batch`'s docstring for the authoritative statement of
  both paths.
"""

from __future__ import annotations

from typing import Annotated, cast, get_args
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from sqlalchemy import and_, delete, func, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import CurrentUserId, SessionDep
from app.core import clock
from app.models import (
    Account,
    Category,
    ImportBatch,
    MerchantTagMap,
    Transaction,
    TransactionLabel,
)
from app.models.instrument import AssetClassStr
from app.parsers.base import InvalidPasswordError, ParserError
from app.schemas import (
    ImportCommit,
    ImportSummary,
    InvestmentCsvImportSummary,
    PendingImportBatch,
    TransactionCandidate,
    TransactionRead,
)
from app.schemas.transactions import ConfidenceStr
from app.services.import_service import (
    AccountNotFoundError,
    NonInrAccountError,
    ParserNotRegisteredError,
    import_statement,
)
from app.services.investment_import_service import import_investment_csv
from app.services.merchant_labels import LABEL_PREFILL_MIN, learn_merchant_memory
from app.services.reconciliation_service import auto_link_cc_bill

# Confidence thresholds (PRD §F3-derived). Locked at the backend so frontend
# tooltip copy doesn't have to know them — the response carries the label.
# "Confident" reuses the single learned-establishment bar LABEL_PREFILL_MIN (N
# confirmations = trusted) rather than a second literal 3: the category
# confidence tint and the label auto-apply gate move together by construction, so
# they can't silently drift apart.
CONFIDENT_MIN: int = LABEL_PREFILL_MIN
UNCERTAIN_MIN: int = 1

# CC statements are ~MBs in practice. The cap defends the worker process
# against malformed multipart uploads or accidental large files — it is
# not a security boundary.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# Allowed asset-class form values for the CSV importer (validated here, not via a
# Literal-typed Form param, so a bad value yields a controlled 422 that doesn't echo
# the rejected input — same input-hygiene reason the upload fields avoid pattern/min).
_ASSET_CLASS_VALUES: frozenset[str] = frozenset(get_args(AssetClassStr))

router = APIRouter(prefix="/imports", tags=["imports"])


@router.post("", response_model=ImportSummary)
def create_import(
    session: SessionDep,
    user_id: CurrentUserId,
    account_id: Annotated[int, Form()],
    file: Annotated[UploadFile, File()],
    # Do not add min_length/pattern — RequestValidationError echoes rejected input.
    password: Annotated[str | None, Form()] = None,
) -> ImportSummary:
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="file too large",
        )
    file_bytes = file.file.read()
    try:
        result = import_statement(
            user_id=user_id,
            account_id=account_id,
            file_bytes=file_bytes,
            password=password,
            session=session,
        )
    # Order matters: InvalidPasswordError is a ParserError subclass.
    except AccountNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="account not found",
        ) from e
    except NonInrAccountError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="statement import requires an INR account",
        ) from e
    except ParserNotRegisteredError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="no parser registered for this issuer/type",
        ) from e
    except InvalidPasswordError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="incorrect or missing PDF password",
        ) from e
    except ParserError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="could not parse statement file",
        ) from e
    session.commit()
    return ImportSummary.model_validate(result)


@router.post("/investments", response_model=InvestmentCsvImportSummary)
def create_investment_csv_import(
    session: SessionDep,
    user_id: CurrentUserId,
    file: Annotated[UploadFile, File()],
    # Applied to rows without an asset_class column (e.g. a single-asset Zerodha
    # tradebook — the user picks the class once here). Validated against the model
    # Literal below; a bad value → 422 without echoing the input.
    asset_class: Annotated[str, Form()],
) -> InvestmentCsvImportSummary:
    """Import a canonical investment-transaction CSV (PRD §F7). Account-less.

    Commits directly (no review queue — investments have no categories). The summary
    carries counts + PII-safe per-row warnings. Generic error detail strings (no PII /
    input echo), same as ``POST /imports``.
    """
    if asset_class not in _ASSET_CLASS_VALUES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="invalid asset_class",
        )
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="file too large",
        )
    file_bytes = file.file.read()
    try:
        result = import_investment_csv(
            user_id=user_id,
            file_bytes=file_bytes,
            default_asset_class=cast("AssetClassStr", asset_class),
            session=session,
        )
    except ParserError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="could not parse investment CSV",
        ) from e
    session.commit()
    return InvestmentCsvImportSummary.model_validate(result)


def _get_batch_or_404(session: Session, *, batch_id: int, user_id: UUID) -> ImportBatch:
    """Scope-check helper. 404 if the batch doesn't exist or belongs elsewhere.

    Generic detail string — does not confirm whether the id exists for a
    different user (enumeration oracle hygiene for v2 multi-user).
    """
    batch = session.scalar(
        select(ImportBatch).where(
            ImportBatch.id == batch_id,
            ImportBatch.user_id == user_id,
        )
    )
    if batch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="import batch not found",
        )
    return batch


def _confidence(prior_matches: int) -> ConfidenceStr:
    if prior_matches >= CONFIDENT_MIN:
        return "confident"
    if prior_matches >= UNCERTAIN_MIN:
        return "uncertain"
    return "none"


@router.get("/pending", response_model=list[PendingImportBatch])
def list_pending_imports(
    session: SessionDep,
    user_id: CurrentUserId,
) -> list[PendingImportBatch]:
    """Open batches (≥1 unconfirmed row) with their pending row count.

    Two-step to stay Postgres-portable: a subquery aggregates the count
    grouped by ``import_batch_id`` alone, then the outer select joins
    ``ImportBatch`` (ordering) + LEFT-joins ``Account`` (label; account_id is
    nullable for investment batches). Projecting the ungrouped ImportBatch /
    Account columns only in the outer query avoids a bare-non-aggregated-column
    ``GROUP BY`` (legal on SQLite, rejected by Postgres).

    Route ordering: this literal path is declared before ``/{batch_id}/...``
    so ``pending`` is never captured as a ``batch_id`` path param.
    """
    pending = (
        select(
            Transaction.import_batch_id.label("batch_id"),
            func.count().label("pending_count"),
        )
        .where(
            Transaction.user_id == user_id,
            Transaction.import_batch_id.is_not(None),
            Transaction.confirmed_at.is_(None),
        )
        .group_by(Transaction.import_batch_id)
        .subquery()
    )

    stmt = (
        select(
            pending.c.batch_id,
            Account.name,
            Account.last4,
            pending.c.pending_count,
        )
        .join(ImportBatch, ImportBatch.id == pending.c.batch_id)
        # Account.user_id belongs in the ON clause, NOT the WHERE: account_id is
        # nullable (backup restore's batch is account-less), so a WHERE predicate would
        # collapse this LEFT JOIN to an inner one and drop those batches from the feed.
        # In the ON clause a foreign account simply fails to match and the label comes
        # back null — the safe degradation, not a cross-user read of name + last4.
        .outerjoin(
            Account,
            and_(Account.id == ImportBatch.account_id, Account.user_id == user_id),
        )
        .where(ImportBatch.user_id == user_id)
        .order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc())
    )

    return [
        PendingImportBatch(
            batch_id=batch_id,
            account_name=account_name,
            account_last4=account_last4,
            pending_count=int(pending_count),
        )
        for batch_id, account_name, account_last4, pending_count in session.execute(stmt).all()
    ]


@router.get("/{batch_id}/candidates", response_model=list[TransactionCandidate])
def list_candidates(
    batch_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> list[TransactionCandidate]:
    """Pending rows of this batch with prior_matches + confidence attached.

    LEFT JOIN on ``(user_id, merchant_normalized, category_id)`` returns
    ``prior_matches = COALESCE(mtm.hit_count, 0)``. For rows where
    ``category_id IS NULL`` (income/transfer; or new-merchant spend),
    ``NULL = NULL`` semantics collapse to "no match" → 0 → ``"none"``.
    Without the ``COALESCE`` Pydantic's ``Field(ge=0)`` would reject null.
    """
    _get_batch_or_404(session, batch_id=batch_id, user_id=user_id)

    stmt = (
        select(
            Transaction,
            func.coalesce(MerchantTagMap.hit_count, 0).label("prior_matches"),
            # A user-authored (pinned) winner prefills at hit_count=1; surface the
            # flag so the picker can render "authored" instead of a low-confidence
            # tint. No joined row → NULL → coalesced to False.
            func.coalesce(MerchantTagMap.pinned, False).label("pinned"),
        )
        # selectinload the labels so TransactionCandidate.labels serializes in one
        # batched query, not N per-row lazy loads.
        .options(selectinload(Transaction.labels))
        .outerjoin(
            MerchantTagMap,
            and_(
                MerchantTagMap.user_id == Transaction.user_id,
                MerchantTagMap.merchant_normalized == Transaction.merchant_normalized,
                MerchantTagMap.category_id == Transaction.category_id,
            ),
        )
        .where(
            Transaction.user_id == user_id,
            Transaction.import_batch_id == batch_id,
            Transaction.confirmed_at.is_(None),
        )
        .order_by(Transaction.date.desc(), Transaction.id.desc())
    )

    return [
        TransactionCandidate(
            **TransactionRead.model_validate(txn).model_dump(),
            prior_matches=int(prior_matches),
            confidence=_confidence(int(prior_matches)),
            pinned=bool(pinned),
        )
        for txn, prior_matches, pinned in session.execute(stmt).all()
    ]


@router.post("/{batch_id}/commit", status_code=status.HTTP_204_NO_CONTENT)
def commit_import_batch(
    batch_id: int,
    payload: ImportCommit,
    session: SessionDep,
    user_id: CurrentUserId,
) -> None:
    """Bulk-stamp ``confirmed_at`` + emit F3 learning signal. Atomic.

    Pre-flight gathers ALL invalid ids in one pass (don't short-circuit) so
    the frontend can recover without a candidates refetch:

    * Missing from the SELECT result (cross-user / cross-batch / non-existent)
      → silently bucketed into ``invalid_ids`` (never leak whether the id
      exists for a different user).
    * Already confirmed → ``invalid_ids``.
    * ``spend`` / ``refund`` with ``category_id IS NULL`` **or a category that was
      archived mid-review** → **defaulted** to the user's spend ``"Other"``
      category (PRD §F5 fallback), not rejected. This keeps a row off a dead
      bucket AND stops pass 3 resurrecting a ``merchant_tag_map`` row for the
      archived category. Only if ``"Other"`` itself is archived/absent do these
      rows fall back to ``invalid_ids``. ``income`` / ``transfer`` keep committing
      with their current category — income may be uncategorized, and pass-2
      auto-link can flip an income CC-payment to a ``transfer``, which must stay
      category-null, so we never stamp Other on it. Defaulted ids are tracked so
      pass 3 skips *category* learning them (a fallback isn't a merchant→category
      decision worth teaching F3); their labels are still learned.

    On any invalid id → 422 with nested
    ``detail={"message": ..., "invalid_ids": [int, ...]}`` and **no writes**
    (the in-memory ``category_id`` defaulting never reaches a flush).

    Commit is **three-pass** — pass 1 stamps every ``confirmed_at`` and
    explicitly flushes, pass 2 runs F4a-1 CC-bill auto-link per row (each in its
    own savepoint via the service), pass 3 emits the F3 (category) + F3a (label)
    learning signal per row via the shared ``learn_merchant_memory`` (which opens
    the per-write ``record_tag`` / ``record_label`` savepoints). The F2
    SAVEPOINT-entry-flush hazard
    ([transactions.py:156-167](backend/app/api/v1/transactions.py#L156-L167))
    means that in a single-pass loop, iteration N's savepoint would flush
    iteration N's pending UPDATE inside the savepoint's scope — coupling
    the row's DB state to the savepoint's success. Pass 1's explicit
    ``session.flush()`` releases every UPDATE to the parent transaction
    *before* any savepoint opens, so a pass-3 conflict-recovery rollback
    reverts only its own write. Pass 2 catches nothing — an F4a constraint
    failure propagates and takes the whole request with it (see below).
    Within pass 3 the same protection holds per write: each
    begin_nested() entry flush lands prior dirty state (e.g. a category
    hit_count bump) on the PARENT transaction before the next savepoint opens,
    so no inter-write flush between the category and label writes is needed.
    Atomic rollback on uncaught exceptions still relies on ``get_db``'s implicit
    ``Session.close()`` cleanup
    ([core/db.py:64-77](backend/app/core/db.py#L64-L77)).

    **Pass order is load-bearing.** F4a runs *before* learning because
    auto-link mutates ``transaction_type`` from ``income`` to ``transfer``
    on matched CC-payment rows. Pass-3's ``should_learn_tag`` gate (inside
    ``learn_merchant_memory``) then sees the post-flip ``transaction_type`` so a
    just-flipped row is excluded from learning. Reversing the passes would leave
    a stale tag-map entry mapping the CC-payment merchant to whatever category
    the user pre-PATCHed onto the row in the review queue.

    **Commit is the SOLE learning point for import rows.** A PATCH of a still
    -pending review-queue row does NOT learn (see ``transactions.py`` —
    ``confirmed_at is not None`` gate); the row is taught here, exactly once.
    So a user-corrected row and a passively-accepted auto-tag both bump the
    rule ``+1`` at commit — no double-count across the PATCH→commit seam, and
    no rule is left behind if the row is discarded before commit.

    Inertia-bump / PRD §F3 deviation: PRD §F3 literal wording is "on user
    tag/retag, upsert into merchant_tag_map" — v1 amends this to also
    bump on commit (each committed row is a distinct user-yes signal).
    A passive-accept of an auto-tag at hit_count=2 promotes the rule to
    confident at hit_count=3. Locked decision; surface in PRD §F3 in a
    follow-up doc PR. A read-time skip-if-confident knob would need to
    thread state from /candidates into /commit; rejected per CLAUDE.md
    §Simplicity.
    """
    _get_batch_or_404(session, batch_id=batch_id, user_id=user_id)

    requested_ids = set(payload.transaction_ids)
    rows = list(
        session.scalars(
            select(Transaction).where(
                Transaction.id.in_(requested_ids),
                Transaction.user_id == user_id,
                Transaction.import_batch_id == batch_id,
            )
        )
    )
    # The user's active spend "Other" category — the fallback for a staged
    # spend/refund row committed without a tag (PRD §F5). Looked up once; None
    # only if the user archived it (then those rows fall back to the 422 guard).
    spend_other_id = session.scalar(
        select(Category.id).where(
            Category.user_id == user_id,
            Category.name == "Other",
            Category.kind == "spend",
            Category.archived_at.is_(None),
        )
    )

    # Active (non-archived) category ids among the rows' current categories. A
    # spend/refund row whose category was archived mid-review is then treated
    # like an untagged one below (default to "Other") — so it never commits to a
    # dead bucket, and pass 3 never resurrects a merchant_tag_map row pointing at
    # the archived category. Scoped to the user, so only *this user's archived*
    # categories are re-bucketed; foreign/absent refs are unreachable in v1
    # (PATCH validates ownership, and auto-tag is user-scoped — prefetch_tag_map
    # restates Category.user_id, which it did NOT until A1.3/A3.3; this comment was
    # asserting the invariant a year before the join enforced it), so this never masks
    # a real integrity problem.
    row_category_ids = {r.category_id for r in rows if r.category_id is not None}
    active_category_ids: set[int] = (
        set(
            session.scalars(
                select(Category.id).where(
                    Category.id.in_(row_category_ids),
                    Category.user_id == user_id,
                    Category.archived_at.is_(None),
                )
            )
        )
        if row_category_ids
        else set()
    )

    returned_ids = {r.id for r in rows}
    invalid_ids: set[int] = requested_ids - returned_ids
    # Rows we defaulted to "Other" — excluded from pass-3 learning below, since a
    # fallback isn't a merchant→category decision worth teaching F3.
    defaulted_ids: set[int] = set()
    for r in rows:
        if r.confirmed_at is not None:
            invalid_ids.add(r.id)
            continue
        # spend/refund with no category — or one whose category was archived
        # mid-review — default to the spend "Other" category rather than being
        # rejected. income/transfer stay as-is: income may commit uncategorized,
        # and auto_link (pass 2) can flip an income CC-payment to a transfer,
        # which MUST stay category-null — so we never stamp Other on it.
        if r.transaction_type in ("spend", "refund") and (
            r.category_id is None or r.category_id not in active_category_ids
        ):
            if spend_other_id is None:
                invalid_ids.add(r.id)  # no Other to fall back to — keep the guard
            else:
                r.category_id = spend_other_id
                defaulted_ids.add(r.id)

    if invalid_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "some transactions are not eligible to commit",
                "invalid_ids": sorted(invalid_ids),
            },
        )

    now = clock.utcnow()
    # Pass 1: stamp every confirmed_at and flush to the parent transaction.
    # Without this explicit flush, pass-2's first savepoint would flush
    # pending UPDATEs inside its own savepoint scope (per the F2 docstring's
    # SAVEPOINT-entry-flush analysis), coupling each row's DB state to the
    # savepoint's success.
    for txn in rows:
        txn.confirmed_at = now
    session.flush()

    # Pass 2: F4a-1 CC-bill auto-link. Per-row savepoint inside the service.
    # Must run BEFORE pass 3 so any transaction_type flip lands before
    # learn_merchant_memory's should_learn_tag eligibility check.
    for txn in rows:
        auto_link_cc_bill(session, user_id=user_id, txn=txn)

    # Pass 3: emit the F3 (category) + F3a (label) learning signal per row via
    # the shared ``learn_merchant_memory`` — the SAME helper POST/PATCH and the
    # seeder use, so commit can't silently diverge from the learning contract.
    # One SELECT prefetches the committed rows' labels (avoids an N-query loop),
    # then one pass per row:
    #   * category — skipped for ``defaulted_ids`` (a fallback to "Other" isn't a
    #     merchant→category decision worth teaching), passed through otherwise;
    #   * labels — never excluded by ``defaulted_ids`` (a row defaulted to "Other"
    #     can still carry legitimate user labels).
    # ``learn_merchant_memory`` applies the shared ``should_learn_tag`` gate once,
    # so rows pass-2 flipped to ``transfer`` (auto-linked CC payments) learn
    # neither — the gate reads the post-flip ``transaction_type``. Each
    # record_tag/record_label opens its own begin_nested() SAVEPOINT whose entry
    # flush lands prior dirty state on the PARENT transaction (protected from a
    # conflict-recovery rollback), so no inter-write flush is needed here.
    labels_by_txn: dict[int, list[int]] = {}
    for txn_id, label_id in session.execute(
        select(TransactionLabel.transaction_id, TransactionLabel.label_id).where(
            TransactionLabel.transaction_id.in_(returned_ids),
            # user_id scope is defense-in-depth (returned_ids is already
            # user+batch-scoped and the composite FK guarantees ownership) —
            # parity with every other user-scoped query in this router.
            TransactionLabel.user_id == user_id,
        )
    ):
        labels_by_txn.setdefault(txn_id, []).append(label_id)
    for txn in rows:
        learn_merchant_memory(
            session,
            user_id=user_id,
            merchant_normalized=txn.merchant_normalized,
            transaction_type=txn.transaction_type,
            category_id=None if txn.id in defaulted_ids else txn.category_id,
            label_ids=labels_by_txn.get(txn.id, ()),
        )

    session.commit()


@router.delete("/{batch_id}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_import_batch(
    batch_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> None:
    """Hard-delete pending rows of this batch; full-cancel deletes the batch row too.

    Re-upload behaviour after this returns:

    * **Full cancel** (zero confirmed survivors): the ``ImportBatch`` row is
      deleted, so the re-upload reconciliation in :func:`import_statement` finds
      no prior batch and re-runs the parser into a fresh batch.
    * **Partial cancel** (some confirmed rows survive): the ``ImportBatch``
      row stays. A re-upload of the same file re-parses and reconciles against
      the DB — the rows deleted by this cancel are missing, so they re-surface
      as pending on the same batch (``imported`` = the resurfaced count). The
      surviving confirmed rows are skipped as still-present.

    TODO(v2-postgres): the SELECT-then-DELETE on the batch row has a TOCTOU
    window only under databases with non-serialized writes. SQLite v1
    serialises write transactions via ``BEGIN IMMEDIATE`` — even with the
    user in two browser tabs (single FastAPI process, distinct sessions per
    request), concurrent ``POST /commit`` blocks on the cancel's transaction
    until the cancel commits or rolls back; the loser then sees the
    deletes and 422s on the pre-flight. Postgres v2 ``READ COMMITTED`` opens
    the window — a concurrent commit could land a confirmed row between
    our count and our delete, and the deferred FK raises at commit time.
    Revisit (explicit lock, ``SELECT ... FOR UPDATE``, or
    ``ON DELETE RESTRICT``) at the v2 swap.
    """
    batch = _get_batch_or_404(session, batch_id=batch_id, user_id=user_id)

    session.execute(
        delete(Transaction).where(
            Transaction.user_id == user_id,
            Transaction.import_batch_id == batch_id,
            Transaction.confirmed_at.is_(None),
        )
    )

    remaining = session.scalar(
        select(func.count())
        .select_from(Transaction)
        .where(
            Transaction.user_id == user_id,
            Transaction.import_batch_id == batch_id,
        )
    )
    if remaining == 0:
        session.delete(batch)

    session.commit()
