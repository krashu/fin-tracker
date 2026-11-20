"""Transaction routes (PRD §F1 read path, §F2 manual entry, PATCH).

* ``GET /api/v1/transactions`` — paginated read, offset/limit, flat shape.
  Clients detect end-of-results by receiving fewer items than ``limit``.
  No ``total`` count — adding one requires a separate ``COUNT(*)``.
  Ordering is ``(date DESC, id DESC)``; ``id DESC`` tiebreaks
  newest-inserted-first within a date.

* ``POST /api/v1/transactions`` — PRD §F2 manual entry. Pre-flights account
  (same user, non-archived, not investment) and category, computes the
  PRD §F4 fingerprint, learns the merchant → category/label memory before
  adding the txn, then commits. A duplicate fingerprint returns 409.

  Load-bearing ordering: label resolution and ``learn_merchant_memory`` run
  BEFORE ``session.add(txn)``. ``resolve_label_names`` (and later ``set_labels``)
  open SAVEPOINTs whose entry-flush would, if the txn were already added, emit
  the Transaction INSERT and fire a fingerprint conflict inside a savepoint exit
  — outside the route's commit-level ``try/except``. The learning upserts are
  single statements (no SAVEPOINT) but keep their slot, so both writes stay on
  the parent transaction and the commit-level handler owns the 409 conversion.

* ``POST /api/v1/transactions/bulk-categorise`` — move up to 500 board rows
  onto one category in a single request (ADR-0004 rules 2b and 4): teaches once
  per canonical merchant, all-or-nothing (422 ``invalid_ids`` before any write).
  Returns ``{updated, taught}``.

* ``POST /api/v1/transactions/bulk-delete`` — hard-delete up to 500 of the caller's rows
  (pending included) in one request, all-or-nothing (422 ``invalid_ids`` before any write);
  each row goes the way a single ``DELETE`` takes it
  (:func:`app.services.transaction_delete.delete_owned_transactions`). 204.

* ``GET /api/v1/transactions/{transaction_id}/past-rows`` — the "apply to past
  rows" offer after a correction: recent board rows of the same canonical merchant
  and type still on a machine-assigned category. The client applies them through
  ``bulk-categorise``, which validates again and teaches +1.

* ``PATCH /api/v1/transactions/{transaction_id}`` — partial-update via
  :class:`TransactionUpdate`. ``model_dump(exclude_unset=True)`` means omitted
  fields leave the DB untouched; explicit ``null`` clears the two nullable
  columns and 422s on the four that are NOT NULL.

  **Every user-visible column is editable** (ADR-0007) — the dedup identity is an
  implementation detail that must never surface as a UI constraint. Editing one of
  the four ADR-0006 identity columns recomputes ``merchant_normalized`` and
  ``fingerprint`` and re-enters the F4 uniqueness contract at ``occurrence = 0``
  (409 on collision), while the immutable ``origin_fingerprint`` keeps that edit
  from resurrecting its own pre-edit version on the next statement re-import. See
  the route docstring for the ordering of the 422 gates.

* ``POST /api/v1/transactions/{transaction_id}/link`` — the manual link (ADR-0002
  rule 11b): pair two equal-and-opposite confirmed rows the F4a auto-link missed. Each
  leg keeps its pre-link type and category for unlink; never teaches.

* ``GET /api/v1/transactions/{transaction_id}/link-candidates`` — the closest 20 rows the
  manual link would accept for this one, so the picker never offers a refusal.

* ``PUT`` / ``DELETE /api/v1/transactions/{transaction_id}/refund-link`` — the
  optional refund → original link (ADR-0009 rule 13). Its own endpoints because
  PATCH takes the seven user-visible fields only (ADR-0006 rule 10).

* ``GET /api/v1/transactions/{transaction_id}/refund-candidates`` — the originals that
  refund may link to (at most 20, ranked), so the picker never offers a refusal.

All queries scope to ``user_id`` from :data:`CurrentUserId` (habit-shaping
for v2 multi-user — single-user v0.1 doesn't strictly need it).

PATCH and POST both pre-flight ``category_id`` against ``categories`` (same
user, not archived) so a bad id surfaces as 422 instead of a generic FK
``IntegrityError`` → 500. The cross-user case is load-bearing: the FK
alone only checks the row exists, not who owns it. On a successful POST, or a
PATCH of a **confirmed (board)** row, the route also learns the merchant →
category/label memory (PRD §F3/§F3a) via :func:`learn_merchant_memory`. One
commit covers the txn write and the learning. A PATCH of a still-**pending**
review-queue row does NOT learn — that row learns once at import commit (pass
3/4), so editing it in the queue can't double-count, orphan a rule on discard,
or self-inflate its ``/candidates`` confidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date as date_t
from datetime import timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import CurrentUserId, SessionDep
from app.core import clock
from app.core.db_errors import is_unique_violation
from app.models import (
    Account,
    Category,
    CategoryKindStr,
    Label,
    PeerEntry,
    Transaction,
    TransactionTypeStr,
)
from app.schemas import (
    BulkCategorise,
    BulkCategoriseResult,
    BulkDelete,
    PastRowsRead,
    TransactionCreate,
    TransactionRead,
    TransactionTransferCreate,
    TransactionUpdate,
    TransferRead,
)
from app.schemas.transactions import RefundLinkSet, TransactionLinkSet, sign_error
from app.services.bulk_categorise import (
    PAST_ROWS_WINDOW_DAYS,
    bulk_categorise,
    past_rows_like,
)
from app.services.category_service import (
    kind_for_type,
    validate_category_ids,
)
from app.services.fingerprint import transaction_fingerprint
from app.services.merchant import normalize_merchant
from app.services.merchant_alias import load_alias_resolver
from app.services.merchant_labels import learn_merchant_memory
from app.services.reconciliation_service import (
    link_candidates,
    manual_link_problem,
    pair_existing_rows,
    release_leg,
)
from app.services.refund_link import (
    NOT_A_REFUND,
    is_refund,
    is_refundable_original,
    refund_candidates,
    refund_link_problem,
)
from app.services.transaction_delete import delete_owned_transactions
from app.services.transaction_labels import resolve_label_names, set_labels_on_transaction
from app.services.transaction_queries import confirmed_only
from app.services.transaction_reads import transaction_reads

router = APIRouter(prefix="/transactions", tags=["transactions"])

# The ADR-0006 identity tuple — the only PATCH fields that force a fingerprint
# recompute (ADR-0007 rule 2). `merchant_raw` rather than `merchant_normalized`
# because the normalized form is derived, never accepted from the body.
_IDENTITY_FIELDS = frozenset({"date", "amount_paise", "merchant_raw", "account_id"})
# What a live transfer pair freezes (rule 7). `category_id` and `labels` are absent
# on purpose: neither participates in the pairing, so both stay editable on a linked
# row — which is what makes the F4a banner's relabel path work without unlinking.
_PAIR_LOCKED_FIELDS = _IDENTITY_FIELDS | {"transaction_type"}


def _to_read(
    session: Session, txns: Sequence[Transaction], *, user_id: UUID
) -> list[TransactionRead]:
    """Project ORM rows onto the wire schema (:func:`transaction_reads`)."""
    return transaction_reads(session, txns, user_id=user_id)


# `response_model=` is deliberate: the wire schema is narrower than the ORM model
# — fingerprint, confirmed_at, etc. are intentionally hidden (see
# schemas/transactions.py). These routes now return `TransactionRead` rather than
# the ORM object because the payload carries two fields the ORM instance cannot
# supply (`category_name` / `category_parent_name`, resolved per-request against
# archived rows too) — the widening the previous note here required before
# changing this. Nothing calls these functions directly; the suite drives them
# over HTTP.
@router.get("", response_model=list[TransactionRead])
def list_transactions(
    session: SessionDep,
    user_id: CurrentUserId,
    account_id: Annotated[int | None, Query(gt=0)] = None,
    category_id: Annotated[int | None, Query(gt=0)] = None,
    label_id: Annotated[int | None, Query(gt=0)] = None,
    transaction_type: Annotated[list[TransactionTypeStr] | None, Query()] = None,
    amount_sign: Annotated[Literal["positive", "negative"] | None, Query()] = None,
    date_from: Annotated[date_t | None, Query()] = None,
    date_to: Annotated[date_t | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TransactionRead]:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="date_from must be <= date_to",
        )
    # Board = committed rows only. Pending rows surface via
    # GET /imports/{batch_id}/candidates. No `?status=` knob — the only
    # board consumer is the frontend and it never wants pending rows here.
    # The board predicate is shared with the F8 dashboard aggregates via
    # confirmed_only() (services/transaction_queries.py).
    #
    # `transaction_type`, by contrast, IS a knob with a real consumer: the
    # /expenses board's Type filter requests spend (its default view, which shows
    # spends and refunds together) or income; transfers stay entry-only and
    # aren't browsed here. It filters server-side to keep offset/limit pagination
    # correct. Lives here, not in confirmed_only() — that predicate stays the
    # shared confirmed-gate and must not couple to the board's display rule.
    #
    # `amount_sign` is deliberately ORTHOGONAL to `transaction_type` rather than
    # folded into it. Since ADR-0009 a refund is a `spend` row with a positive
    # amount, so the board's "Refunds" view is expressed as the composition
    # `?transaction_type=spend&amount_sign=positive` — there is no `refund`
    # value to request any more. Keeping it a separate axis means the two
    # compose for any future sign-scoped view without re-cutting the type enum.
    stmt = confirmed_only(select(Transaction).where(Transaction.user_id == user_id))
    if account_id is not None:
        stmt = stmt.where(Transaction.account_id == account_id)
    if transaction_type:
        # `if transaction_type:` not `is not None`: a truthy guard so an empty
        # list never reaches `.in_([])` (which emits `WHERE 1=0` → zero rows).
        # An empty list isn't reachable over HTTP (omitting the param → None;
        # `?transaction_type=` → `[""]` → 422 on the Literal), so this is
        # belt-and-braces for a programmatic `[]` caller. The Literal type
        # bounds each value (unknown → 422), so no manual validation is needed.
        stmt = stmt.where(Transaction.transaction_type.in_(transaction_type))
    if amount_sign is not None:
        # Zero is rejected at every write path, so these two are exhaustive and
        # `positive` is exactly the refund set when composed with type=spend.
        stmt = stmt.where(
            Transaction.amount_paise > 0
            if amount_sign == "positive"
            else Transaction.amount_paise < 0
        )
    if category_id is not None:
        # Drilldown filter for the F8 spend-by-category surface.
        # Matches the category itself OR any child subcategories where parent_id == category_id.
        # Tenant isolation: Category.user_id == user_id ensures cross-user queries yield empty.
        cat_subquery = select(Category.id).where(
            Category.user_id == user_id,
            or_(Category.id == category_id, Category.parent_id == category_id),
        )
        stmt = stmt.where(Transaction.category_id.in_(cat_subquery))
    if label_id is not None:
        # F3a label filter. `.any()` → EXISTS subquery (one link per (txn, label)),
        # so there is no join-row duplication and offset/limit pagination stays
        # honest — unlike an inner JOIN, which could multiply rows.
        stmt = stmt.where(Transaction.labels.any(Label.id == label_id))
    if date_from is not None:
        stmt = stmt.where(Transaction.date >= date_from)
    if date_to is not None:
        stmt = stmt.where(Transaction.date <= date_to)
    # selectinload the labels so TransactionRead.labels serializes in one extra
    # batched query, not N per-row lazy loads.
    stmt = (
        stmt.options(selectinload(Transaction.labels))
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return _to_read(session, list(session.scalars(stmt)), user_id=user_id)


def _assert_category_id_or_422(
    session: Session,
    *,
    category_id: int,
    user_id: UUID,
    kind: CategoryKindStr,
) -> None:
    """Raise 422 if ``category_id`` is unknown / cross-user / archived / wrong-kind.

    Used by both POST and PATCH so a bad id surfaces with a useful message
    instead of a generic FK ``IntegrityError`` → 500. The cross-user check
    is load-bearing: the FK alone only proves the row exists, not who owns
    it. ``kind`` (derived from the row's ``transaction_type`` via
    :func:`kind_for_type`) rejects a spend row pointing at an income category
    and vice-versa — previously a UI-only invariant, now enforced at the API for
    every category-assignment path. Caller null-checks ``category_id`` first.

    Thin single-id wrapper around
    :func:`app.services.category_service.validate_category_ids` (the shared
    validity rule, reused by ``rules.py`` rule authoring).
    """
    if category_id not in validate_category_ids(
        session, category_ids=[category_id], user_id=user_id, kind=kind
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="category not found, archived, or wrong kind for this transaction",
        )


def _assert_account_or_422(session: Session, *, account_id: int, user_id: UUID) -> None:
    """Raise 422 unless ``account_id`` is owned, non-archived, non-investment and INR.

    The four create-path account checks, shared with ``PATCH /transactions`` now that
    ADR-0007 rule 6 makes ``account_id`` editable — a re-filed row must clear exactly
    the same bar as a newly-created one. Cross-user ids 422 rather than being silently
    honoured (root ``AGENTS.md`` §Tenant isolation rule 3); the row is fetched whole because
    two of the checks read columns, not just existence.
    """
    account = session.scalar(
        select(Account).where(
            Account.id == account_id,
            Account.user_id == user_id,
            Account.archived_at.is_(None),
        )
    )
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="account not found or archived",
        )
    if account.type == "investment":
        # F7 owns investment txns (separate table). Silent acceptance here
        # would corrupt F8 dashboard signed-sums once they land.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="transactions cannot be posted to investment accounts",
        )
    if account.currency != "INR":
        # v1 spending is INR-only; a USD account's cents would be summed as INR
        # paise by the currency-blind F8 dashboard aggregates. Defense in depth —
        # AccountCreate blocks USD accounts at the API, but a seed/model-created
        # USD account is still reachable here.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="spending transactions must be on an INR account",
        )


def _is_fingerprint_conflict(orig: BaseException | None) -> bool:
    """True when an ``IntegrityError.orig`` is the dedup-key uniqueness violation.

    The constraint is the 4-column ``UNIQUE(user_id, account_id, fingerprint,
    occurrence)``, ``uq_transactions_user_account_fingerprint`` (ADR-0006 rule 6).
    Shared by ``POST /transactions``, ``POST /transactions/transfer`` and ``PATCH
    /transactions/{id}`` so the dedup → 409 mapping lives in one place; delegates the
    dialect-aware matching to :func:`app.core.db_errors.is_unique_violation`. Postgres
    matches the index name; SQLite matches ``columns=`` as a subset — every listed
    ``table.col`` token must appear among the failing columns, so the three listed
    here identify the constraint without naming ``occurrence``.
    """
    return is_unique_violation(
        orig,
        index_name="uq_transactions_user_account_fingerprint",
        columns=["transactions.user_id", "transactions.account_id", "transactions.fingerprint"],
    )


@router.post("", response_model=TransactionRead, status_code=status.HTTP_201_CREATED)
def create_transaction(
    payload: TransactionCreate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> TransactionRead:
    # Account pre-flight: ownership + non-archived + non-investment + INR.
    _assert_account_or_422(session, account_id=payload.account_id, user_id=user_id)

    if payload.category_id is not None:
        _assert_category_id_or_422(
            session,
            category_id=payload.category_id,
            user_id=user_id,
            kind=kind_for_type(payload.transaction_type),
        )

    # normalize_merchant(None) would crash on .lower(); a no-merchant row
    # normalizes to "" so the fingerprint (PRD §F4) still hashes a string.
    merchant_normalized = normalize_merchant(payload.merchant_raw) if payload.merchant_raw else ""
    fp = transaction_fingerprint(
        txn_date=payload.date,
        amount_paise=payload.amount_paise,
        normalized_merchant=merchant_normalized,
        account_id=payload.account_id,
    )

    # Resolve labels (get-or-create) BEFORE adding the txn — resolve_label_names
    # opens begin_nested() SAVEPOINTs whose flush would otherwise emit the txn
    # INSERT and surface a fingerprint dup outside the commit-level 409 handler.
    # set_labels runs after the flush below, once txn.id exists.
    resolved_labels = resolve_label_names(session, user_id=user_id, names=payload.labels)

    # F3 + F3a learning, BEFORE session.add(txn) — ordering kept. record_tag /
    # record_label are single-statement upserts (no SAVEPOINT), but the txn INSERT
    # must still surface at session.commit() inside the route's try/except (→ 409),
    # not mid-learning (→ 500). Both writes share the parent transaction, so a 409
    # rolls the tag/label increments back atomically.
    # The shared should_learn_tag gate keeps income/transfer (hand-classified) out
    # of the spend→category/label maps; a #salary on an income row still persists
    # via set_labels below but is never learned.
    learn_merchant_memory(
        session,
        user_id=user_id,
        resolver=load_alias_resolver(session, user_id=user_id),
        merchant_normalized=merchant_normalized,
        transaction_type=payload.transaction_type,
        category_id=payload.category_id,
        label_ids=[label.id for label in resolved_labels],
    )

    txn = Transaction(
        user_id=user_id,
        account_id=payload.account_id,
        date=payload.date,
        amount_paise=payload.amount_paise,
        transaction_type=payload.transaction_type,
        merchant_raw=payload.merchant_raw,
        merchant_normalized=merchant_normalized,
        category_id=payload.category_id,
        fingerprint=fp,
        source="manual",
        import_batch_id=None,
        # F2 rows skip the review queue — they're born on the board. The column
        # itself stays nullable so genuinely pending import rows are
        # distinguishable from F2 rows; the stamp is set here, not as a
        # column server_default.
        confirmed_at=clock.naive_utcnow(),
    )
    session.add(txn)

    try:
        # Flush inside the 409 guard: it assigns txn.id (needed to link the
        # labels) AND is where a duplicate fingerprint surfaces — keeping it here
        # maps that dup to 409, not a 500 from a later flush.
        session.flush()
        set_labels_on_transaction(session, txn=txn, labels=resolved_labels)
        session.commit()
    except IntegrityError as e:
        session.rollback()
        if _is_fingerprint_conflict(e.orig):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="transaction already exists",
            ) from e
        raise
    session.refresh(txn)
    return _to_read(session, [txn], user_id=user_id)[0]


@router.post("/transfer", response_model=TransferRead, status_code=status.HTTP_201_CREATED)
def create_transfer(
    payload: TransactionTransferCreate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> TransferRead:
    """F2 manual transfer — two cross-linked ``transfer`` rows, one money movement.

    Second writer of ``transfer_pair_id`` (first: F4a ``auto_link_cc_bill``).
    Signs are server-derived: source = ``-amount_paise`` (outflow), dest =
    ``+amount_paise`` (inflow). Both legs are born ``transaction_type="transfer"``,
    ``source="manual"``, ``confirmed_at=now()`` (on the board, not the review
    queue), ``category_id=None``.

    Atomicity mirrors :func:`create_transaction`: the route owns the commit and
    the ``IntegrityError`` → 409 mapping. No ``begin_nested()`` — both rows are
    new in one request-scoped transaction, so a failure rolls the whole request
    back. ``auto_link_cc_bill`` savepoints instead, but for flush attribution
    rather than recovery: it catches nothing either, so an unrecognised
    conflict rolls the whole request back on both paths. The difference is only
    that this one maps a fingerprint collision to a 409 first, because it sits
    on a user-facing boundary. The two flushes are mandatory: autoincrement ids are
    unknown until flush #1, and the composite FK + no-self-pair CHECK can only
    validate after ``transfer_pair_id`` is set (flush #2).

    Known v1 limitation: two identical same-day transfers between the same two
    accounts produce identical leg fingerprints → the second returns 409
    (consistent with PRD §F4). Merchant labels snapshot the account names at
    creation; an account rename does not backfill them.
    """

    def _load(account_id: int, label: str) -> Account:
        # Per-leg pre-flight: ownership + non-archived + non-investment. Mirrors
        # create_transaction's account guard, applied once per leg.
        account = session.scalar(
            select(Account).where(
                Account.id == account_id,
                Account.user_id == user_id,
                Account.archived_at.is_(None),
            )
        )
        if account is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{label} account not found or archived",
            )
        if account.type == "investment":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="transfers cannot involve investment accounts",
            )
        return account

    source_account = _load(payload.source_account_id, "source")
    dest_account = _load(payload.dest_account_id, "destination")

    # Both legs of one movement must share a unit: mixing INR/USD would yield
    # incomparable magnitudes and a meaningless dashboard signed-sum. Checked
    # first for the more specific message. USD accounts stay reachable via
    # seed/model construction (AccountCreate now rejects them at the API), so
    # this is a real boundary check, not speculative FX.
    if source_account.currency != dest_account.currency:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="transfer accounts must share a currency",
        )
    # v1 money is INR-only. The legs share a currency by the check above, so
    # testing the source alone rejects a matched USD↔USD transfer.
    if source_account.currency != "INR":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="transfers must be on INR accounts",
        )

    source_merchant = f"Transfer to {dest_account.name}"
    dest_merchant = f"Transfer from {source_account.name}"
    now = clock.naive_utcnow()
    source = Transaction(
        user_id=user_id,
        account_id=source_account.id,
        date=payload.date,
        amount_paise=-payload.amount_paise,
        transaction_type="transfer",
        merchant_raw=source_merchant,
        merchant_normalized=normalize_merchant(source_merchant),
        category_id=None,
        fingerprint=transaction_fingerprint(
            txn_date=payload.date,
            amount_paise=-payload.amount_paise,
            normalized_merchant=normalize_merchant(source_merchant),
            account_id=source_account.id,
        ),
        source="manual",
        import_batch_id=None,
        confirmed_at=now,
    )
    dest = Transaction(
        user_id=user_id,
        account_id=dest_account.id,
        date=payload.date,
        amount_paise=payload.amount_paise,
        transaction_type="transfer",
        merchant_raw=dest_merchant,
        merchant_normalized=normalize_merchant(dest_merchant),
        category_id=None,
        fingerprint=transaction_fingerprint(
            txn_date=payload.date,
            amount_paise=payload.amount_paise,
            normalized_merchant=normalize_merchant(dest_merchant),
            account_id=dest_account.id,
        ),
        source="manual",
        import_batch_id=None,
        confirmed_at=now,
    )

    session.add_all([source, dest])
    try:
        session.flush()  # assigns ids; (user_id, account_id, fingerprint) uniqueness fires
        source.transfer_pair_id = dest.id  # symmetry — ADR-0002 §3 (service-layer responsibility)
        dest.transfer_pair_id = source.id
        session.flush()  # composite FK + no-self-pair CHECK validate here
        session.commit()
    except IntegrityError as e:
        # Rolls back to the transaction root, discarding BOTH pending legs — so a
        # one-sided collision (e.g. only the dest leg) never orphans the other.
        session.rollback()
        if _is_fingerprint_conflict(e.orig):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="transaction already exists",
            ) from e
        raise
    session.refresh(source)
    session.refresh(dest)
    source_read, dest_read = _to_read(session, [source, dest], user_id=user_id)
    return TransferRead(source=source_read, dest=dest_read)


@router.patch("/{transaction_id}", response_model=TransactionRead)
def update_transaction(
    transaction_id: int,
    payload: TransactionUpdate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> TransactionRead:
    """Partial update. Every user-visible column is editable (ADR-0007).

    Two cost classes, one endpoint. ``transaction_type`` / ``category_id`` /
    ``labels`` are free — the type is absent from the ADR-0006 hash payload. The
    four identity columns (``date``, ``amount_paise``, ``merchant_raw``,
    ``account_id``) trigger a recompute, but only when one of them **actually
    changes**, so a no-op PATCH stays a no-op.

    Order below is deliberate: every 422 gate runs on the merged state *before* the
    first ``setattr``, so a rejected request leaves the row untouched.

    * **Transfer guard** (ADR-0006 rule 15) — a paired row rejects identity/type
      *changes* with 422 (unlink first); ``category_id`` / ``labels`` stay editable,
      since neither participates in the pairing. An unpaired row may change its type
      to or from ``transfer`` (ADR-0002 rule 7d): the patch writes no pair column.
      The guard compares against the stored row, so re-sending a stored value — the
      ``transfer`` type of an unpaired transfer row, or a leg's own amount — is a
      no-op, not a 422.
    * **Sign/type is a merged pair** (rule 4) — shared with
      ``TransactionCreate._check_sign`` via :func:`sign_error`, because the schema
      validator cannot see the stored row.
    * **A refund link stays true** (ADR-0009 rule 13) — when sign or type is in
      play, a linked refund must stay a positive spend and a linked original a
      negative one; otherwise 422 until the link is removed.
    * **Category kind follows the POST-patch type** (rule 5). A kind flip must carry
      a compatible ``category_id`` — or an explicit ``null`` — in the same request.
      No silent clearing: the picker already knows the required kind, and clearing
      would destroy a choice the user made.
    * **Recompute** (rule 3) — ``merchant_normalized`` then ``fingerprint``, letting
      the unique index adjudicate (``IntegrityError`` → 409). ``occurrence`` resets
      to 0 only when the fingerprint actually moved: a PATCH is a lone operation
      that cannot count a file's multiset, so it must *ask* rather than carry an
      ordinal (ADR-0006 rule 4). Guarding the reset on the hash rather than on the
      raw input is what keeps a cosmetic re-casing of ``merchant_raw`` — which
      normalizes to the same string — from vacating an occupied slot in a duplicate
      group and 409-ing on itself.
    * ``origin_fingerprint`` is **never** touched here (rule 17): it answers which
      statement line produced the row, so an edit must not make the row look
      deleted to the importer. The first identity edit of a row that has one instead
      freezes the pre-edit inputs in the ``origin_*`` columns (later edits leave them),
      so the origin stays recomputable on any install.

    Pending and confirmed rows are equally editable (rule 10) — the loader scopes on
    ``id`` + ``user_id`` alone, symmetric with DELETE. The review queue is a
    confirmation gate, not a lock.
    """
    txn = session.scalar(
        select(Transaction).where(
            Transaction.id == transaction_id,
            Transaction.user_id == user_id,
        )
    )
    if txn is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="transaction not found",
        )
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        # Empty body — no DB round-trip, no spurious updated_at bump.
        return _to_read(session, [txn], user_id=user_id)[0]

    # Labels are a relationship (replace-set), not a column — pull them out before
    # the column setattr loop below (setattr(txn, "labels", [<str>]) on the
    # viewonly relationship would break). `labels_present` distinguishes "omitted"
    # (leave alone) from "sent" (replace exactly, incl. [] / null = clear).
    labels_present = "labels" in updates
    label_names = updates.pop("labels", None) or []

    # --- Rule 7: transfer guard -------------------------------------------------
    # Fires on a real change only: a body that re-sends a stored value (a client
    # echoing the row back) is a no-op, not an edit (ADR-0006 rule 15).
    pair_locked_changes = {
        f for f in _PAIR_LOCKED_FIELDS & updates.keys() if updates[f] != getattr(txn, f)
    }
    if txn.transfer_pair_id is not None and pair_locked_changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="unlink this transfer before editing its identity fields or type",
        )

    # --- Rule 2: did an identity input actually change? -------------------------
    identity_changed = any(updates[f] != getattr(txn, f) for f in _IDENTITY_FIELDS & updates.keys())

    # --- Rule 6: an account change re-runs the create path's four checks ---------
    if "account_id" in updates and updates["account_id"] != txn.account_id:
        _assert_account_or_422(session, account_id=updates["account_id"], user_id=user_id)

    # --- Rule 4: sign and type validated as a post-patch PAIR -------------------
    # Only when the request actually puts that pair in play (ADR-0006 rule 12).
    # Validating an untouched pair would strand a row the caller is not changing. Since
    # ADR-0009 every type takes either sign, so the only pair sign_error rejects is a
    # zero amount — which no API path writes, but a labels-only PATCH on such a row
    # must still not 422 on state it never touched.
    merged_type = updates.get("transaction_type", txn.transaction_type)
    if {"transaction_type", "amount_paise"} & updates.keys():
        sign_problem = sign_error(merged_type, updates.get("amount_paise", txn.amount_paise))
        if sign_problem is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=sign_problem
            )

    # --- ADR-0009 rule 13: a refund link stays true -----------------------------
    # Only when the request puts sign or type in play. A linked refund must remain a
    # positive spend, and a linked original a negative one; the link is removed first
    # (DELETE /transactions/{id}/refund-link), never silently dropped here.
    if {"transaction_type", "amount_paise"} & updates.keys():
        merged_amount = updates.get("amount_paise", txn.amount_paise)
        if txn.refund_of_id is not None and not is_refund(merged_type, merged_amount):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="remove the refund link before changing this row's sign or type",
            )
        if not is_refundable_original(merged_type, merged_amount) and session.scalar(
            select(
                exists().where(
                    Transaction.user_id == user_id,
                    Transaction.refund_of_id == txn.id,
                )
            )
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="a refund links to this row; remove that link before changing "
                "its sign or type",
            )

    # --- ADR-0017 Rule 4 & 8: transaction allocation guard & peer link direction -
    # If the transaction is linked to peer debt entries, the new absolute magnitude
    # must not be less than the total allocated paise, and the sign must not invert
    # (fronted debt vs repayment credit).
    if {"transaction_type", "amount_paise"} & updates.keys():
        merged_amount = updates.get("amount_paise", txn.amount_paise)
        allocated_peer_paise = (
            session.scalar(
                select(func.coalesce(func.sum(PeerEntry.amount_paise), 0)).where(
                    PeerEntry.user_id == user_id,
                    PeerEntry.transaction_id == txn.id,
                )
            )
            or 0
        )
        if allocated_peer_paise > 0:
            if abs(merged_amount) < allocated_peer_paise:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=(
                        f"transaction amount cannot be less than allocated peer entries "
                        f"({allocated_peer_paise} paise)"
                    ),
                )
            if (merged_amount > 0) != (txn.amount_paise > 0):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="cannot invert transaction sign while peer entries remain linked",
                )

    # --- Rule 5: the category kind follows the POST-patch type ------------------
    merged_kind = kind_for_type(merged_type)
    if "category_id" in updates and updates["category_id"] is not None:
        _assert_category_id_or_422(
            session,
            category_id=updates["category_id"],
            user_id=user_id,
            kind=merged_kind,
        )
    elif (
        # Absent, not null: an explicit `"category_id": null` is the sanctioned way
        # through a kind flip and must NOT land here.
        "category_id" not in updates
        and merged_kind != kind_for_type(txn.transaction_type)
        and txn.category_id is not None
    ):
        # The type flipped kind and the body kept the old category. Reject rather
        # than silently clearing (Alternative 5) — the frontend picker already
        # filters by categoryKindForType, so one round-trip covers it.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="changing the transaction type requires a compatible category_id, or null",
        )

    # Stash pre-PATCH category for the no-change short-circuit below. Read
    # before setattr so we compare the user's actual decision (changed vs
    # re-confirmed), not the post-write state.
    prev_category_id = txn.category_id

    # Only TransactionUpdate fields reach `updates`, so auto_category_id (absent
    # from that schema) is never written here — and must NOT be: it's the frozen
    # import suggestion, and a category_id PATCH is exactly the "edit" the
    # acceptance-rate metric (GET /dashboards/tagging-stats) counts as not-kept.
    if identity_changed and txn.origin_fingerprint is not None and txn.origin_date is None:
        # ADR-0006 rule 17: the first identity edit of an imported row freezes the origin's
        # inputs, so its hash stays recomputable (and portable) after the edit. Later edits
        # leave them alone; origin_fingerprint itself is never touched.
        txn.origin_date, txn.origin_amount_paise = txn.date, txn.amount_paise
        txn.origin_merchant_normalized, txn.origin_account_id = (
            txn.merchant_normalized,
            txn.account_id,
        )

    for field, value in updates.items():
        setattr(txn, field, value)
    # ADR-0002 rule 9: a real category change on a linked leg is the user's newer choice, so
    # the category the link set aside is discarded in the same UPDATE (the DB CHECK demands
    # it). An echo of the stored value is not a change (ADR-0006 rule 15).
    if (
        "category_id" in updates
        and updates["category_id"] != prev_category_id
        and txn.transfer_pair_id is not None
    ):
        txn.pre_link_category_id = None

    # --- Rule 3: recompute the derived identity columns -------------------------
    if identity_changed:
        # Same None → "" convention as the create path, so a merchant cleared on
        # PATCH hashes exactly like one omitted on POST.
        txn.merchant_normalized = normalize_merchant(txn.merchant_raw) if txn.merchant_raw else ""
        recomputed = transaction_fingerprint(
            txn_date=txn.date,
            amount_paise=txn.amount_paise,
            normalized_merchant=txn.merchant_normalized,
            account_id=txn.account_id,
        )
        if recomputed != txn.fingerprint:
            txn.fingerprint = recomputed
            txn.occurrence = 0
        try:
            # Flush the column edits HERE, not at the commit below. The label
            # helpers (resolve_label_names, set_labels) open begin_nested() SAVEPOINTs
            # whose entry-flush would otherwise emit this UPDATE inside a savepoint
            # owned by the label conflict predicate, which re-raises past a
            # commit-level handler → 500 instead of 409. POST solves the same hazard
            # by ordering label work before session.add; PATCH cannot, because rule 8
            # needs the post-patch merchant and type. Mirrors create_transfer's
            # two-flush shape.
            session.flush()
        except IntegrityError as e:
            session.rollback()
            if _is_fingerprint_conflict(e.orig):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="transaction already exists",
                ) from e
            raise

    # F3a labels — replace the txn's label set when `labels` was in the body.
    # set_labels_on_transaction returns the ids it inserted (diffed against the
    # fresh join rows), which feeds additions-only learning below — removals are
    # not un-learned (no decay in v1, matching merchant_tag_map). One commit
    # covers the column edits, label links, and learning.
    added_label_ids: set[int] = set()
    if labels_present:
        resolved_labels = resolve_label_names(session, user_id=user_id, names=label_names)
        added_label_ids = set_labels_on_transaction(session, txn=txn, labels=resolved_labels)

    # PRD §F3 + §F3a learning via the shared helper. Learn ONLY for confirmed
    # (board) rows — a pending review-queue edit learns exclusively at import
    # commit (imports.py pass 3/4); teaching it here too would double-count across
    # the PATCH→commit seam, orphan a rule on discard, or make GET /candidates
    # count the user's own in-session pick as prior history. Category: skip when
    # unchanged (a re-PATCH of the same value isn't a new decision → no hit_count
    # inflation under frontend retries); labels: additions only. The helper applies
    # the shared spend-type gate against the POST-patch type and merchant — both
    # read after the setattr loop, so re-typing a row to income in the same request
    # correctly stops it learning, and a merchant rename teaches the corrected name
    # (ADR-0007 rule 8: learning is unchanged, and these two consequences are
    # accepted rather than patched with new teach sites).
    new_category_id = updates.get("category_id")
    learn_category = (
        "category_id" in updates
        and new_category_id is not None
        and new_category_id != prev_category_id
        and txn.confirmed_at is not None
    )
    learn_labels = bool(added_label_ids) and txn.confirmed_at is not None
    if learn_category or learn_labels:
        learn_merchant_memory(
            session,
            user_id=user_id,
            resolver=load_alias_resolver(session, user_id=user_id),
            merchant_normalized=txn.merchant_normalized,
            transaction_type=txn.transaction_type,
            category_id=new_category_id if learn_category else None,
            label_ids=added_label_ids if learn_labels else (),
        )

    session.commit()
    session.refresh(txn)
    return _to_read(session, [txn], user_id=user_id)[0]


@router.delete("/{transaction_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_transaction(
    transaction_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> None:
    """Hard delete (PRD §F4a-4 resolution path for re-imported / corrected rows).

    The row goes through :func:`app.services.transaction_delete.delete_owned_transactions`,
    which nulls the refund links pointing at it (ADR-0009 rule 13) and releases its partner
    (ADR-0002 rules 5 and 9). Hard, not soft: the only references to ``transactions.id`` are the
    two self-FKs and ``transaction_labels`` (CASCADE). The project-wide rule: soft-delete for
    entities other rows reference (Account, Category), hard-delete for leaves.

    **Pending rows are deletable.** Loader scopes on ``id`` + ``user_id``
    only — no ``confirmed_at`` gate. Symmetric with PATCH
    (:func:`update_transaction`); a user noticing junk in the review
    queue can DELETE it directly without going through the per-batch
    candidates flow.
    """
    txn = session.scalar(
        select(Transaction).where(
            Transaction.id == transaction_id,
            Transaction.user_id == user_id,
        )
    )
    if txn is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="transaction not found",
        )
    delete_owned_transactions(session, user_id=user_id, ids=[txn.id])
    session.commit()
    return None


@router.post("/{transaction_id}/unlink", status_code=status.HTTP_204_NO_CONTENT)
def unlink_transaction(
    transaction_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> None:
    """Break a transfer pair (PRD §F4a-1 "break the link if the auto-detection got it wrong").

    Clears ``transfer_pair_id`` on BOTH legs symmetrically and restores each
    leg's ``pre_link_type`` where a link made it a transfer (ADR-0002 rule 9);
    a leg born as a transfer (F2) has none and stays ``"transfer"``. The same
    release the shared delete gives a deleted leg's partner. Idempotent: a row with no
    ``transfer_pair_id`` is a 204 no-op. Origin-agnostic — clears an F4a
    auto-link or an F2 manual-transfer pair alike; the F2-vs-F4a distinction is
    a product-surface concern (the unlink affordance lives only on the F4a
    banner per PRD §F4a-1), not a runtime guard.

    The inverse of ``POST /transactions/transfer``. Clearing
    ``transfer_pair_id`` to NULL can't trip the constraints — a composite-FK
    tuple with a NULL member isn't checked (MATCH SIMPLE), and
    ``ck_transactions_no_self_pair`` and ``ck_transactions_paired_is_transfer``
    are NULL-tolerant, the latter because each row's UPDATE clears its pair in
    the same statement that restores its type — so order is
    irrelevant and no ``begin_nested`` / savepoint is needed (contrast
    ``create_transfer``, which two-flushes because it SETS a non-null pair that
    must validate).
    """
    txn = session.scalar(
        select(Transaction).where(
            Transaction.id == transaction_id,
            Transaction.user_id == user_id,
        )
    )
    if txn is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="transaction not found",
        )

    # Symmetric clear (ADR-0002 §3). The partner is loaded by txn.transfer_pair_id
    # and user-scoped; the `is not None` guard is the type narrowing on
    # session.scalar (the composite FK already guarantees the partner exists when
    # transfer_pair_id is non-null). Skipped entirely when already unpaired → 204 no-op.
    if txn.transfer_pair_id is not None:
        partner = session.scalar(
            select(Transaction).where(
                Transaction.id == txn.transfer_pair_id,
                Transaction.user_id == user_id,
            )
        )
        release_leg(txn)
        if partner is not None:
            release_leg(partner)

    session.commit()
    return None


def _load_owned(session: Session, *, transaction_id: int, user_id: UUID) -> Transaction | None:
    return session.scalar(
        select(Transaction).where(
            Transaction.id == transaction_id,
            Transaction.user_id == user_id,
        )
    )


@router.post("/{transaction_id}/link", status_code=status.HTTP_204_NO_CONTENT)
def link_transaction(
    transaction_id: int,
    payload: TransactionLinkSet,
    session: SessionDep,
    user_id: CurrentUserId,
) -> None:
    """Manually pair two existing confirmed rows as a transfer (ADR-0002 rule 11b).

    "Paid your card bill elsewhere?": for when the F4a auto-link missed. The row in the path
    must be the caller's (404 otherwise, ADR-0003 rule 8); a missing or foreign ``partner_id``
    is a 422 (rule 9). Then :func:`manual_link_problem` decides eligibility (422 with its
    reason), and :func:`pair_existing_rows` writes the pair, each leg's pre-link type and
    category. A link is not a categorisation, so it never teaches (ADR-0004).
    """
    txn = _load_owned(session, transaction_id=transaction_id, user_id=user_id)
    if txn is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="transaction not found")
    partner = _load_owned(session, transaction_id=payload.partner_id, user_id=user_id)
    if partner is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="partner transaction not found",
        )
    if (
        problem := manual_link_problem(session, user_id=user_id, txn=txn, partner=partner)
    ) is not None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problem)
    pair_existing_rows(session, txn, partner)
    session.commit()


@router.get("/{transaction_id}/link-candidates", response_model=list[TransactionRead])
def get_link_candidates(
    transaction_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> list[TransactionRead]:
    """The rows the manual link would accept for this one: at most 20, nearest date first
    (ADR-0002 rule 11b). Runs the same eligibility rule as ``POST .../link``, so a listed row
    is never refused. A row that cannot be linked itself returns ``[]``; one the caller does
    not own is a 404 (ADR-0003 rule 8).
    """
    txn = _load_owned(session, transaction_id=transaction_id, user_id=user_id)
    if txn is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="transaction not found")
    return _to_read(session, link_candidates(session, user_id=user_id, txn=txn), user_id=user_id)


@router.put("/{transaction_id}/refund-link", status_code=status.HTTP_204_NO_CONTENT)
def set_refund_link(
    transaction_id: int,
    payload: RefundLinkSet,
    session: SessionDep,
    user_id: CurrentUserId,
) -> None:
    """Link a refund to the spend it reverses (ADR-0009 rule 13).

    Every check runs before the write: the row must be the caller's (404 otherwise,
    ADR-0003 rule 8) and a refund — a positive ``spend``; the original must be the
    caller's (422 otherwise, rule 9), a negative ``spend``, and confirmed. Confirmed,
    because the review-queue discard deletes pending rows only, so an original cannot
    disappear under its refund that way. Several refunds may point at one original;
    amounts are not summed against it — the link is informational.
    """
    txn = _load_owned(session, transaction_id=transaction_id, user_id=user_id)
    if txn is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="transaction not found")
    if not is_refund(txn.transaction_type, txn.amount_paise):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=NOT_A_REFUND,
        )
    original = _load_owned(session, transaction_id=payload.original_id, user_id=user_id)
    if original is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="original transaction not found",
        )
    if (problem := refund_link_problem(txn, original)) is not None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=problem)
    txn.refund_of_id = original.id
    session.commit()


@router.delete("/{transaction_id}/refund-link", status_code=status.HTTP_204_NO_CONTENT)
def delete_refund_link(
    transaction_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> None:
    """Remove a refund's link to its original. Idempotent: an unlinked row is a 204
    no-op; another user's row is a 404 (ADR-0003 rule 8)."""
    txn = _load_owned(session, transaction_id=transaction_id, user_id=user_id)
    if txn is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="transaction not found")
    txn.refund_of_id = None
    session.commit()


@router.get("/{transaction_id}/refund-candidates", response_model=list[TransactionRead])
def get_refund_candidates(
    transaction_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> list[TransactionRead]:
    """The originals this refund may link to: at most 20, the current original first, then
    the same merchant and amount (ADR-0009 rule 13). Runs the same eligibility rule as
    ``PUT .../refund-link``, so a listed row is never refused. A row that is not a refund
    returns ``[]``; one the caller does not own is a 404 (ADR-0003 rule 8).
    """
    txn = _load_owned(session, transaction_id=transaction_id, user_id=user_id)
    if txn is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="transaction not found")
    rows = refund_candidates(
        session,
        user_id=user_id,
        refund=txn,
        resolver=load_alias_resolver(session, user_id=user_id),
    )
    return _to_read(session, rows, user_id=user_id)


@router.post("/bulk-categorise", response_model=BulkCategoriseResult)
def bulk_categorise_transactions(
    payload: BulkCategorise,
    session: SessionDep,
    user_id: CurrentUserId,
) -> BulkCategoriseResult:
    """Categorise many board rows at once; one teach per canonical merchant.

    Every check runs before any write, so the request is all-or-nothing: an unknown,
    foreign or archived category → 422; any id that is missing or foreign, still
    pending (staged rows never teach, ADR-0004 rule 3), or of a kind the category
    does not accept → 422 ``{message, invalid_ids}``. Duplicate ids collapse.
    """
    category_ids = validate_category_ids(
        session, category_ids=[payload.category_id], user_id=user_id
    )
    if payload.category_id not in category_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="category not found, archived, or wrong kind for this transaction",
        )
    category = session.scalar(
        select(Category).where(Category.id == payload.category_id, Category.user_id == user_id)
    )
    assert category is not None  # validated above

    wanted = set(payload.transaction_ids)
    rows = session.scalars(
        select(Transaction).where(Transaction.id.in_(wanted), Transaction.user_id == user_id)
    ).all()
    found = {row.id: row for row in rows}
    invalid = {
        txn_id
        for txn_id in wanted
        if (row := found.get(txn_id)) is None
        or row.confirmed_at is None
        or kind_for_type(row.transaction_type) != category.kind
    }
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "some transactions cannot be categorised",
                "invalid_ids": sorted(invalid),
            },
        )

    outcome = bulk_categorise(
        session,
        user_id=user_id,
        rows=rows,
        category_id=payload.category_id,
        resolver=load_alias_resolver(session, user_id=user_id),
    )
    session.commit()
    return BulkCategoriseResult(updated=outcome.updated, taught=outcome.taught)


@router.post("/bulk-delete", status_code=status.HTTP_204_NO_CONTENT)
def bulk_delete_transactions(
    payload: BulkDelete,
    session: SessionDep,
    user_id: CurrentUserId,
) -> None:
    """Hard-delete many rows at once, all-or-nothing (ADR-0003 rule 9).

    Any id that is missing or another user's → 422 ``{message, invalid_ids}`` before any
    write. Pending rows are accepted, as the single ``DELETE`` accepts them. Duplicate ids
    collapse. Deletion semantics live in
    :func:`app.services.transaction_delete.delete_owned_transactions`.
    """
    wanted = set(payload.transaction_ids)
    found = set(
        session.scalars(
            select(Transaction.id).where(Transaction.id.in_(wanted), Transaction.user_id == user_id)
        )
    )
    if invalid := wanted - found:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "some transactions cannot be deleted",
                "invalid_ids": sorted(invalid),
            },
        )
    delete_owned_transactions(session, user_id=user_id, ids=wanted)
    session.commit()


@router.get("/{transaction_id}/past-rows", response_model=PastRowsRead)
def get_past_rows(
    transaction_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> PastRowsRead:
    """Recent rows like this one that "apply to past rows" would recategorise.

    404 for a missing or foreign row. A pending or uncategorised source is not an
    error — it simply offers nothing. Window and cap: :data:`PAST_ROWS_WINDOW_DAYS`
    and ``BULK_MAX``.
    """
    source = _load_owned(session, transaction_id=transaction_id, user_id=user_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="transaction not found")
    resolver = load_alias_resolver(session, user_id=user_id)
    rows = past_rows_like(
        session,
        user_id=user_id,
        source=source,
        resolver=resolver,
        since=clock.today() - timedelta(days=PAST_ROWS_WINDOW_DAYS),
    )
    return PastRowsRead(
        merchant=resolver.canonical(source.merchant_normalized),
        category_id=source.category_id,
        transaction_ids=[row.id for row in rows],
    )
