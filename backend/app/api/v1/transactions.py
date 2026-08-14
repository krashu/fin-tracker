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

  Load-bearing ordering: ``learn_merchant_memory`` runs BEFORE
  ``session.add(txn)``. It opens per-write SAVEPOINTs whose entry-flush would,
  if the txn were already added, emit the Transaction INSERT and fire a
  fingerprint conflict inside a savepoint exit — outside the route's
  commit-level ``try/except``. Learning first keeps that flush a no-op on the
  still-absent txn; both writes stay on the parent transaction and the
  commit-level handler owns the 409 conversion.

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
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import or_, select
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
    Transaction,
    TransactionTypeStr,
)
from app.schemas import (
    TransactionCreate,
    TransactionRead,
    TransactionTransferCreate,
    TransactionUpdate,
    TransferRead,
)
from app.schemas.transactions import sign_error
from app.services.category_service import (
    kind_for_type,
    resolve_category_labels,
    validate_category_ids,
)
from app.services.fingerprint import transaction_fingerprint
from app.services.merchant import normalize_merchant
from app.services.merchant_labels import learn_merchant_memory
from app.services.transaction_labels import resolve_label_names, set_labels_on_transaction
from app.services.transaction_queries import confirmed_only

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
    """Project ORM rows onto the wire schema, naming each row's category.

    The names cannot be read off the ORM object: ``Transaction`` has no
    ``category`` relationship — ``labels`` is documented as its only one, and
    ADR-0012 keeps ORM cascades off ``categories`` after a ``delete-orphan``
    there turned a reparent into silent row deletion. So they arrive from one
    batched query that deliberately ignores ``archived_at``
    (:func:`app.services.category_service.resolve_category_labels`), which is
    what lets a transaction on an archived category still render its real name
    instead of "Uncategorized".
    """
    names = resolve_category_labels(
        session,
        category_ids=[t.category_id for t in txns if t.category_id is not None],
        user_id=user_id,
    )
    reads: list[TransactionRead] = []
    for t in txns:
        read = TransactionRead.model_validate(t)
        if t.category_id is not None:
            # A miss means the id is not owned by this user, so it is left unnamed
            # rather than reaching for a name across the tenant boundary.
            read.category_name, read.category_parent_name = names.get(t.category_id, (None, None))
        reads.append(read)
    return reads


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
    honoured (``backend/CLAUDE.md`` tenant rule 3); the row is fetched whole because
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
    """True when an ``IntegrityError.orig`` is the
    ``(user_id, account_id, fingerprint)`` uniqueness violation (PRD §F4 dedup).

    Shared by ``POST /transactions`` and ``POST /transactions/transfer`` so the
    dedup → 409 mapping lives in one place; delegates the dialect-aware matching
    to :func:`app.core.db_errors.is_unique_violation`.
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

    # F3 + F3a learning, BEFORE session.add(txn) — load-bearing ordering.
    # learn_merchant_memory opens record_tag/record_label begin_nested()
    # SAVEPOINTs; their entry-flush (SessionTransaction._take_snapshot →
    # Session.flush() for nested savepoints) must stay a no-op on the still-absent
    # txn INSERT, so a fingerprint dup surfaces at session.commit() inside the
    # route's try/except (→ 409), not mid-learning (→ 500). Both writes share the
    # parent transaction, so a 409 rolls the tag/label increments back atomically.
    # The shared should_learn_tag gate keeps income/transfer (hand-classified) out
    # of the spend→category/label maps; a #salary on an income row still persists
    # via set_labels below but is never learned.
    learn_merchant_memory(
        session,
        user_id=user_id,
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
        confirmed_at=clock.utcnow(),
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
    now = clock.utcnow()
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
    return TransferRead(
        source=TransactionRead.model_validate(source),
        dest=TransactionRead.model_validate(dest),
    )


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

    * **Transfer guard** (rule 7) — a paired row rejects identity/type edits with
      422 (unlink first); ``category_id`` / ``labels`` stay editable, since neither
      participates in the pairing. ``transfer`` is not a valid *target* either:
      pairs are born via ``POST /transactions/transfer``, and a lone leg minted here
      would violate ADR-0002's exactly-two-pairing invariant.
    * **Sign/type is a merged pair** (rule 4) — shared with
      ``TransactionCreate._check_sign`` via :func:`sign_error`, because the schema
      validator cannot see the stored row.
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
    * ``origin_fingerprint`` is **never** touched here (rule 9): it answers which
      statement line produced the row, so an edit must not make the row look
      deleted to the importer.

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
    if updates.get("transaction_type") == "transfer":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="cannot change transaction_type to transfer; use POST /transactions/transfer",
        )
    if txn.transfer_pair_id is not None and _PAIR_LOCKED_FIELDS & updates.keys():
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
    # Only when the request actually puts that pair in play. Validating an untouched
    # pair would strand a row the caller is not changing: `backup_csv` checks the type
    # vocabulary and that the amount parses, but NOT the sign pairing, and a
    # hand-edited zip is its declared threat model — so a stored `income` carrying a
    # negative amount is reachable, and a labels-only PATCH on it must not 422.
    # (A stored `spend` with a positive amount needs no such exemption since
    # ADR-0009: that IS a refund, and sign_error accepts it outright.)
    merged_type = updates.get("transaction_type", txn.transaction_type)
    if {"transaction_type", "amount_paise"} & updates.keys():
        sign_problem = sign_error(merged_type, updates.get("amount_paise", txn.amount_paise))
        if sign_problem is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=sign_problem
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
    for field, value in updates.items():
        setattr(txn, field, value)

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
            # Flush the column edits HERE, not at the commit below. The label and
            # learning helpers open begin_nested() SAVEPOINTs whose entry-flush would
            # otherwise emit this UPDATE inside a savepoint owned by the label/tag
            # conflict predicate, which re-raises past a commit-level handler → 500
            # instead of 409. POST solves the same hazard by ordering learning before
            # session.add; PATCH cannot, because rule 8 needs the post-patch merchant
            # and type. Mirrors create_transfer's two-flush shape.
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

    **Hard, not soft.** Transactions are leaves in v0.1 — nothing else
    FKs to ``transactions.id`` except ``transfer_pair_id``. The
    project-wide rule: soft-delete for entities other rows reference
    (Account, Category), hard-delete for leaves.

    **Paired-row pre-flight.** F4a-1 (CC-bill auto-link) is a writer of
    ``transfer_pair_id``. Deleting one half of a pair without nulling
    the partner first would trip the composite FK from migration 0005.
    Solution: null the partner's ``transfer_pair_id`` in the same
    session before the delete. The partner's ``transaction_type``
    stays ``"transfer"`` — PRD doesn't pin a restoration value and no
    provenance column exists to know whether the original was
    ``"spend"`` or ``"income"``. The user can manually relabel via a
    future widened PATCH; cosmetic anomaly, not a data-integrity bug.

    **No ``merchant_tag_map`` write.** Per PRD §F3 ``hit_count`` is a
    monotonic positive signal. PATCH writes to the map because PATCH
    expresses a *new positive decision* about the merchant→category
    binding; DELETE expresses *no decision* (the user is removing a
    row, not retracting their judgement about what the merchant means).
    Not symmetric ops on the same signal.

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

    # Paired-row pre-flight — see docstring. Null the partner's
    # transfer_pair_id in the same outer transaction as the delete.
    if txn.transfer_pair_id is not None:
        partner = session.scalar(
            select(Transaction).where(
                Transaction.id == txn.transfer_pair_id,
                Transaction.user_id == user_id,
            )
        )
        if partner is not None:
            partner.transfer_pair_id = None

    session.delete(txn)
    session.commit()
    return None


@router.post("/{transaction_id}/unlink", status_code=status.HTTP_204_NO_CONTENT)
def unlink_transaction(
    transaction_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> None:
    """Break a transfer pair (PRD §F4a-1 "break the link if the auto-detection got it wrong").

    Clears ``transfer_pair_id`` on BOTH legs symmetrically; leaves
    ``transaction_type="transfer"`` on both — no provenance column exists to
    restore the pre-link spend/income type, consistent with the DELETE-leg
    precedent above. Idempotent: a row with no ``transfer_pair_id`` is a 204
    no-op. Origin-agnostic — clears an F4a auto-link or an F2 manual-transfer
    pair alike; the F2-vs-F4a distinction is a product-surface concern (the
    unlink affordance lives only on the F4a banner per PRD §F4a-1), not a
    runtime guard.

    The inverse of ``POST /transactions/transfer``. Clearing
    ``transfer_pair_id`` to NULL can't trip the constraints — a composite-FK
    tuple with a NULL member isn't checked (MATCH SIMPLE), and
    ``ck_transactions_no_self_pair`` is itself NULL-tolerant — so order is
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
        txn.transfer_pair_id = None
        if partner is not None:
            partner.transfer_pair_id = None

    session.commit()
    return None
