"""``/api/v1/imports`` — upload a statement (PRD §F1) + review/commit lifecycle.

* ``POST /imports`` — upload a statement file. Thin route: the body is read in memory under
  the size cap (``app/api/uploads.py``, ADR-0010 rule 13), then it delegates to
  :func:`app.services.import_service.import_statement`, commits, and returns the summary.
  Exception → HTTP mapping is hardcoded with **generic** detail strings — no
  ``account_id``, password, issuer, or
  exception args echoed — so a future Sentry / log-capture path doesn't
  surface user-supplied input through the response body either.

  Mapping:
  * :class:`AccountNotFoundError` → 422 ``"account not found or archived"``
  * :class:`NonInrAccountError` → 422 ``"statement import requires an INR account"``
  * :class:`ParserNotRegisteredError` → 422 ``"no parser registered for this issuer/type"``
  * :class:`InvalidPasswordError` → 422 ``"incorrect or missing PDF password"``
  * :class:`StatementMerchantTooLongError` → 422 naming the row's position (ADR-0006 rule 3b)
  * :class:`RunningBalanceMismatchError` → 422
    ``"running balance does not match on page {page}, row {row}"`` (a bank row whose printed
    balance does not follow; the position is the only payload, ADR-0010 rule 11)
  * :class:`ParserError` → 422 ``"could not parse statement file"``

  Any other exception raised inside a parser is logged by type only (``statement_parser_crashed``)
  and becomes a :class:`ParserError` (``parse_statement``), so it takes the last row above.

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
  ``prior_matches`` + ``confidence`` attached, resolved through the user's
  alias table (ADR-0011 merchant-alias layer, Phase A3) rather than a raw
  ``merchant_tag_map`` join — see :func:`list_candidates`. The frontend's
  review queue surface. Why not ``GET /transactions?import_batch_id=X&status=pending``:
  candidates carry ``prior_matches`` / ``confidence`` which ``TransactionRead``
  deliberately omits per its docstring.

* ``POST /imports/{batch_id}/commit`` — bulk-stamp ``confirmed_at`` on the
  ``transaction_ids`` and delete the ``discard_ids`` in the same request. Atomic: any
  invalid id → 422 with ``invalid_ids: [...]``, no writes. Body is id lists only — category
  edits the user made in the queue must be PATCHed to ``/transactions/{id}`` before commit.
  Commit reads ``category_id`` from the DB, not the request.

* ``DELETE /imports/{batch_id}`` — cancel: hard-delete pending rows of this
  batch. If zero rows remain on the batch after delete, hard-delete the
  ``ImportBatch`` row too so re-upload re-runs the parser. Partial cancel
  (any confirmed rows survive) keeps the batch row; a re-upload of the same
  file then re-parses and reconciles against the DB, so the cancelled rows
  **re-surface as pending** on that same batch. See
  :func:`cancel_import_batch`'s docstring for the authoritative statement of
  both paths.

* ``GET /imports/{batch_id}/reconciliation`` — recompute + persist this
  batch's statement-balance reconciliation (PRD §F1/§F4a) and return the
  full breakdown. Declared **after** ``/pending`` so that literal path is
  never captured as a ``batch_id``. See :func:`get_batch_reconciliation`.

* ``GET`` / ``POST /imports/samples`` — the bundled synthetic sample statements (ADR-0003
  rule 15). Open to guests, who cannot upload; ``POST`` stages one into a matching account
  and returns the same summary as an upload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Annotated, Literal, cast, get_args
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import and_, delete, func, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import CurrentUserId, NonGuestUserId, SessionDep
from app.api.uploads import read_upload
from app.core import clock
from app.core.log_config import get_logger
from app.models import (
    Account,
    Category,
    CategoryKindStr,
    ImportBatch,
    Transaction,
    TransactionLabel,
)
from app.models.instrument import AssetClassStr
from app.parsers import UnsupportedFileFormatError
from app.parsers.base import InvalidPasswordError, ParserError, RunningBalanceMismatchError
from app.schemas import (
    BatchReconciliation,
    ImportCommit,
    ImportSummary,
    InvestmentCsvImportSummary,
    InvestmentCsvUploadForm,
    PendingImportBatch,
    SampleImportCreate,
    SampleStatementRead,
    StatementUploadForm,
    TransactionCandidate,
)
from app.schemas.transactions import ConfidenceStr
from app.services.category_service import (
    CASHBACK_CATEGORY_NAME,
    default_category_id,
    fallback_category_id,
)
from app.services.import_service import (
    AccountNotFoundError,
    NonInrAccountError,
    ParserNotRegisteredError,
    StatementMerchantTooLongError,
    import_statement,
    is_cashback_credit,
)
from app.services.investment_import_service import import_investment_csv
from app.services.merchant_alias import load_alias_resolver
from app.services.merchant_labels import LABEL_PREFILL_MIN, learn_merchant_memory
from app.services.portfolio_analytics_service import invalidate_portfolio_snapshots
from app.services.reconciliation_service import (
    auto_link_cc_bill,
    card_bill_transfer_gate,
    is_card_bill_debit_row,
    is_card_payment_row,
    is_cc_payment,
    reconcile_batch,
    rows_removed_since_import,
)
from app.services.sample_statements import (
    SAMPLES,
    SampleAccountMismatchError,
    UnknownSampleError,
    eligible_account_ids,
    import_sample_statement,
)
from app.services.tag_service import is_seeded, prefetch_tag_strength
from app.services.transaction_delete import delete_owned_transactions
from app.services.transaction_reads import transaction_reads

# Confidence thresholds (PRD §F3-derived). Locked at the backend so frontend
# tooltip copy doesn't have to know them — the response carries the label.
# "Confident" reuses the single learned-establishment bar LABEL_PREFILL_MIN (N
# confirmations = trusted) rather than a second literal 3: the category
# confidence tint and the label auto-apply gate move together by construction, so
# they can't silently drift apart.
CONFIDENT_MIN: int = LABEL_PREFILL_MIN
UNCERTAIN_MIN: int = 1

# CC statements are ~MBs in practice. The cap covers the whole request body (multipart
# overhead is a few hundred bytes) and is enforced while streaming, not after buffering
# (app/api/uploads.py). It defends the worker process against malformed multipart uploads or
# accidental large files — it is not a security boundary.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# FastAPI infers no body schema for a route that reads its own multipart stream, so the docs
# are stated here.
_STATEMENT_UPLOAD_BODY = {
    "required": True,
    "content": {
        "multipart/form-data": {
            "schema": {
                "type": "object",
                "required": ["account_id", "file"],
                "properties": {
                    "account_id": {"type": "integer"},
                    "file": {"type": "string", "format": "binary"},
                    "password": {"type": "string"},
                },
            }
        }
    },
}
_INVESTMENT_CSV_UPLOAD_BODY = {
    "required": True,
    "content": {
        "multipart/form-data": {
            "schema": {
                "type": "object",
                "required": ["asset_class", "file"],
                "properties": {
                    "asset_class": {"type": "string"},
                    "file": {
                        "type": "string",
                        "format": "binary",
                        "description": "Investment transaction CSV or Excel (.xlsx) file",
                    },
                },
            }
        }
    },
}
_TOO_LARGE_RESPONSE: dict[int | str, dict[str, str]] = {
    413: {"description": "Request body over the upload cap"}
}


@dataclass(frozen=True, slots=True)
class StatementUpload:
    account_id: int
    # Never rendered into a repr / traceback.
    password: str | None = field(repr=False)
    file_bytes: bytes = field(repr=False)


async def read_statement_upload(request: Request) -> StatementUpload:
    fields, file_bytes = await read_upload(
        request, StatementUploadForm, max_bytes=MAX_UPLOAD_BYTES, max_fields=2
    )
    return StatementUpload(
        account_id=fields.account_id, password=fields.password, file_bytes=file_bytes
    )


@dataclass(frozen=True, slots=True)
class InvestmentCsvUpload:
    asset_class: str
    file_bytes: bytes = field(repr=False)


async def read_investment_csv_upload(request: Request) -> InvestmentCsvUpload:
    fields, file_bytes = await read_upload(
        request, InvestmentCsvUploadForm, max_bytes=MAX_UPLOAD_BYTES, max_fields=1
    )
    return InvestmentCsvUpload(asset_class=fields.asset_class, file_bytes=file_bytes)


# Allowed asset-class form values for the CSV importer (validated here, not via a
# Literal-typed Form param, so a bad value yields a controlled 422 that doesn't echo
# the rejected input — same input-hygiene reason the upload fields avoid pattern/min).
_ASSET_CLASS_VALUES: frozenset[str] = frozenset(get_args(AssetClassStr))

logger = get_logger(__name__)

router = APIRouter(prefix="/imports", tags=["imports"])


@router.post(
    "",
    response_model=ImportSummary,
    openapi_extra={"requestBody": _STATEMENT_UPLOAD_BODY},
    responses=_TOO_LARGE_RESPONSE,
)
def create_import(
    session: SessionDep,
    # Declared before the upload so 401/403 fire before a body byte is read.
    user_id: NonGuestUserId,
    upload: Annotated[StatementUpload, Depends(read_statement_upload)],
) -> ImportSummary:
    try:
        result = import_statement(
            user_id=user_id,
            account_id=upload.account_id,
            file_bytes=upload.file_bytes,
            password=upload.password,
            session=session,
        )
    # Order matters: InvalidPasswordError is a ParserError subclass.
    except AccountNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="account not found or archived",
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
    except RunningBalanceMismatchError as e:
        # Position only: never statement text (ADR-0010 rules 11 and 13).
        logger.warning("statement_running_balance_mismatch", page=e.page, row=e.row)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"running balance does not match on page {e.page}, row {e.row}",
        ) from e
    except StatementMerchantTooLongError as e:
        # Row position only, already logged by the service: never statement text.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)) from e
    except ParserError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="could not parse statement file",
        ) from e
    session.commit()
    return ImportSummary.model_validate(result)


@router.post(
    "/investments",
    response_model=InvestmentCsvImportSummary,
    openapi_extra={"requestBody": _INVESTMENT_CSV_UPLOAD_BODY},
    responses=_TOO_LARGE_RESPONSE,
)
def create_investment_csv_import(
    session: SessionDep,
    user_id: NonGuestUserId,
    # ``asset_class`` is applied to rows without an asset_class column (e.g. a single-asset
    # Zerodha tradebook — the user picks the class once). Validated against the model
    # Literal below; a bad value → 422 without echoing the input.
    upload: Annotated[InvestmentCsvUpload, Depends(read_investment_csv_upload)],
) -> InvestmentCsvImportSummary:
    """Import a canonical investment-transaction CSV (PRD §F7). Account-less.

    Commits directly (no review queue — investments have no categories). The summary
    carries counts + PII-safe per-row warnings. Generic error detail strings (no PII /
    input echo), same as ``POST /imports``.
    """
    if upload.asset_class not in _ASSET_CLASS_VALUES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="invalid asset_class",
        )
    try:
        result = import_investment_csv(
            user_id=user_id,
            file_bytes=upload.file_bytes,
            default_asset_class=cast("AssetClassStr", upload.asset_class),
            session=session,
        )
    except UnsupportedFileFormatError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(e),
        ) from e
    except ParserError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="could not parse investment CSV",
        ) from e
    if result.txns_imported > 0:
        invalidate_portfolio_snapshots(session, user_id=user_id, from_date=date.min)
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


def _candidate_strength(
    strength: dict[tuple[str, int], tuple[int, bool]],
    *,
    canonical: str,
    category_id: int | None,
) -> tuple[int, ConfidenceStr, bool]:
    """(prior_matches, confidence, pinned) for one candidate row.

    ``category_id=None`` (income/transfer, or a spend row with no category yet)
    can never be a key in ``strength`` — every stored merchant_tag_map row has a
    real category — so it degrades to "none" the same way the old LEFT JOIN's
    ``NULL = NULL`` did. A ``(canonical, category_id)`` pair ABSENT from
    ``strength`` is "no rule at all" -> ``"none"``; one PRESENT at
    ``hit_count == 0`` and not pinned is a seeded, never-confirmed row
    (ADR-0011 rule 11) -> ``"seeded"``. Collapsing that distinction with a
    ``.get(key, (0, False))`` default is exactly what
    :func:`app.services.tag_service.prefetch_tag_strength`'s docstring forbids.

    A pinned row is never "seeded" (:func:`~app.services.tag_service.is_seeded`):
    pinning a seed row is the one way to reach ``hit_count == 0, pinned=True``,
    because :func:`app.services.tag_service.pin_tag` deliberately never bumps
    ``hit_count`` on an existing row. It reads ``(0, "none", True)`` and
    ``TagPicker`` checks ``pinned`` ahead of ``confidence``, so it renders as
    user-authored instead of an unconfirmed dictionary suggestion.
    """
    entry = strength.get((canonical, category_id)) if category_id is not None else None
    if entry is None:
        return 0, "none", False
    hit_count, pinned = entry
    if is_seeded(hit_count=hit_count, pinned=pinned):
        return 0, "seeded", False
    return hit_count, _confidence(hit_count), pinned


@router.get("/pending", response_model=list[PendingImportBatch])
def list_pending_imports(
    session: SessionDep,
    user_id: CurrentUserId,
) -> list[PendingImportBatch]:
    """Open batches (≥1 unconfirmed row) with their pending row count.

    Two-step to stay Postgres-portable: a subquery aggregates the count
    grouped by ``import_batch_id`` alone, then the outer select joins
    ``ImportBatch`` (ordering + ``reconciliation_delta_paise``) + LEFT-joins
    ``Account`` (label; account_id is nullable for investment batches).
    Projecting the ungrouped ImportBatch / Account columns only in the outer
    query avoids a bare-non-aggregated-column ``GROUP BY`` (legal on SQLite,
    rejected by Postgres) — ``reconciliation_delta_paise`` is one such column,
    selected here rather than added to the subquery's ``GROUP BY``.

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
            ImportBatch.reconciliation_delta_paise,
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
            reconciliation_delta_paise=reconciliation_delta_paise,
        )
        for batch_id, account_name, account_last4, pending_count, reconciliation_delta_paise in (
            session.execute(stmt).all()
        )
    ]


def _cc_payment_candidate(txn: Transaction, *, bank_batch: bool) -> bool:
    """Whether the review queue should offer the card-bill link on this row (PRD §F4a-1).

    On a card batch it is an ``income`` credit naming a bill payment; on a bank batch it is a
    debit naming one. The card clause is gated on the batch kind, so a bank income row that
    merely says "PAYMENT RECEIVED" is never flagged.
    """
    if bank_batch:
        return is_card_bill_debit_row(txn)
    return is_card_payment_row(txn)


@router.get("/{batch_id}/candidates", response_model=list[TransactionCandidate])
def list_candidates(
    batch_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> list[TransactionCandidate]:
    """Pending rows of this batch with prior_matches + confidence attached.

    Resolved through the user's alias table (ADR-0011 merchant-alias layer,
    Phase A3) rather than a raw ``merchant_tag_map`` join: one resolver load +
    one :func:`app.services.tag_service.prefetch_tag_strength` call (both
    user-scoped, same cost shape as the import-time prefetch), then each row
    looks up ``(resolver.canonical(merchant_normalized), category_id)`` in the
    strength map via :func:`_candidate_strength`. This is deliberately the
    same resolution path :func:`app.services.import_service.import_statement`
    uses to prefill — a candidate that resolves via an alias to a merchant the
    user *has* confirmed (under a different raw descriptor) now shows that
    history, which the old exact-string LEFT JOIN could never see.

    Note the archived-category filter now reaches this endpoint too:
    ``prefetch_tag_strength`` (via ``_aggregate_tag_rows``) excludes rows whose
    category is archived, so a candidate pointing at a since-archived category
    reads ``"none"`` here — the old LEFT JOIN touched no ``Category`` row and
    kept the raw ``hit_count`` regardless. That is a deliberate side effect
    (more consistent with the real prefill), not a regression.
    """
    batch = _get_batch_or_404(session, batch_id=batch_id, user_id=user_id)
    bank_batch = (
        batch.account_id is not None
        and session.scalar(
            select(Account.type).where(Account.id == batch.account_id, Account.user_id == user_id)
        )
        == "bank"
    )
    commits_as_transfer = card_bill_transfer_gate(session, user_id=user_id, batch=batch)

    stmt = (
        select(Transaction)
        # selectinload the labels so TransactionCandidate.labels serializes in one
        # batched query, not N per-row lazy loads.
        .options(selectinload(Transaction.labels))
        .where(
            Transaction.user_id == user_id,
            Transaction.import_batch_id == batch_id,
            Transaction.confirmed_at.is_(None),
        )
        .order_by(Transaction.date.desc(), Transaction.id.desc())
    )

    resolver = load_alias_resolver(session, user_id=user_id)
    strength = prefetch_tag_strength(session, user_id=user_id, resolver=resolver)

    txns = list(session.scalars(stmt))
    # `TransactionCandidate` extends `TransactionRead`, so it inherits the category names and
    # `card_bill_link_offered`: build them with the shared builder, once for the whole batch,
    # rather than letting the queue ship them as permanent nulls. A suggested category is
    # normally active, but a batch left in the queue while its category is archived
    # would otherwise render "Uncategorized" over a real suggestion.
    reads = transaction_reads(session, txns, user_id=user_id)

    candidates = []
    for txn, read in zip(txns, reads, strict=True):
        canonical = resolver.canonical(txn.merchant_normalized)
        prior_matches, confidence, pinned = _candidate_strength(
            strength, canonical=canonical, category_id=txn.category_id
        )
        candidates.append(
            TransactionCandidate(
                **read.model_dump(),
                prior_matches=prior_matches,
                confidence=confidence,
                pinned=pinned,
                cc_payment_candidate=_cc_payment_candidate(txn, bank_batch=bank_batch),
                uncategorised_commits_as_transfer=commits_as_transfer(txn),
            )
        )
    return candidates


@router.post("/{batch_id}/commit", status_code=status.HTTP_204_NO_CONTENT)
def commit_import_batch(
    batch_id: int,
    payload: ImportCommit,
    session: SessionDep,
    user_id: CurrentUserId,
) -> None:
    """Bulk-stamp ``confirmed_at`` + emit F3 learning signal, and delete ``discard_ids``. Atomic.

    Discard ids follow the same eligibility rules as commit ids (pending, this user's, this
    batch's) and an id in both lists is invalid. Discarded rows go through the shared delete
    (:func:`delete_owned_transactions`, ADR-0002 rule 5) after the checks and before pass 1,
    and never teach: every pass below runs over the committed rows only. The batch row stays
    even when a discard leaves it empty.

    Pre-flight gathers ALL invalid ids in one pass (don't short-circuit) so
    the frontend can recover without a candidates refetch:

    * Missing from the SELECT result (cross-user / cross-batch / non-existent)
      → silently bucketed into ``invalid_ids`` (never leak whether the id
      exists for a different user).
    * Already confirmed → ``invalid_ids``.
    * A **spend** row (refunds included — a refund is a positive ``spend``,
      ADR-0009) with ``category_id IS NULL`` **or a category that was archived
      mid-review** → **defaulted**, not rejected: the row lands on the user's
      spend ``"Other"`` (PRD §F5 fallback), which always exists (ADR-0012 rule
      8). This keeps a row off a dead bucket AND stops pass 3 resurrecting a
      ``merchant_tag_map`` row for the archived category.
    * An **income** row whose category was archived mid-review → **defaulted**
      to the income ``"Other"`` the same way. An income row with
      ``category_id IS NULL`` stays null: income may commit uncategorized, and
      a pass-2 auto-link moves a leg's category to ``pre_link_category_id``
      (ADR-0002 rule 9), so a defaulted income row would be saved as a pre-link
      category for no reason. ``transfer`` rows keep their category.
    * A bank debit naming a card-bill payment with ``category_id IS NULL`` commits as an
      unpaired ``transfer`` with no category instead, when ``card_bill_transfer_gate`` says
      so: a live card names the bank "Paid from" and a committed statement of it covers the
      debit's date (ADR-0002 rule 7b). Pass 2 may link it; left unlinked it stays out of
      spend. Otherwise it is an ordinary spend and defaults to "Other". A card credit naming a
      bill payment with ``category_id IS NULL`` commits as an unpaired ``transfer`` the same way,
      always (rule 7c), so it never counts as income.
    * A positive ``spend`` row naming cashback (``is_cashback_credit``) that is
      still in the spend ``"Cashback"`` ``import_statement`` pre-filled it into
      (ADR-0013 rule 2) keeps that category and counts as **defaulted**: a
      keyword guess, not a merchant→category decision (ADR-0004 rule 5).

    Defaulted ids are tracked so pass 3 skips *category* learning them (a
    fallback isn't a merchant→category decision worth teaching F3, ADR-0004
    rule 5); their labels are still learned.

    On any invalid id → 422 with nested
    ``detail={"message": ..., "invalid_ids": [int, ...]}`` and **no writes**
    (the in-memory ``category_id`` defaulting never reaches a flush).

    Commit is **three-pass** — pass 1 stamps every ``confirmed_at`` and
    explicitly flushes, pass 2 runs F4a-1 CC-bill auto-link per row, card and bank
    rows alike (each in its own savepoint via the service), pass 3 emits the F3 (category) +
    F3a (label) learning signal per row via the shared ``learn_merchant_memory`` (single-statement
    ``record_tag`` / ``record_label`` upserts, no savepoints). The F2
    SAVEPOINT-entry-flush hazard
    ([transactions.py:156-167](backend/app/api/v1/transactions.py#L156-L167))
    means that in a single-pass loop, iteration N's savepoint would flush
    iteration N's pending UPDATE inside the savepoint's scope — coupling
    the row's DB state to the savepoint's success. Pass 1's explicit
    ``session.flush()`` releases every UPDATE to the parent transaction
    *before* any savepoint opens, so a savepoint rollback (pass 2)
    reverts only its own write. Pass 2 catches nothing — an F4a constraint
    failure propagates and takes the whole request with it (see below).
    Pass 3 opens no savepoint at all: each upsert is one statement on the parent
    transaction, so no inter-write flush is needed.
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
    batch = _get_batch_or_404(session, batch_id=batch_id, user_id=user_id)

    commit_ids = set(payload.transaction_ids)
    discard_ids = set(payload.discard_ids)
    requested_ids = commit_ids | discard_ids
    fetched = list(
        session.scalars(
            select(Transaction).where(
                Transaction.id.in_(requested_ids),
                Transaction.user_id == user_id,
                Transaction.import_batch_id == batch_id,
            )
        )
    )
    # Only the committed rows go through the cashback / category / defaulting / learning
    # passes below; the discarded ones are only checked, then deleted.
    rows = [r for r in fetched if r.id in commit_ids]
    discard_rows = [r for r in fetched if r.id in discard_ids]
    # The user's fallback "Other" per kind (PRD §F5), looked up lazily — only
    # when a row needs one — and at most once per kind. It always exists
    # (ADR-0012 rule 8), so there is no "no fallback" branch.
    fallback_ids: dict[CategoryKindStr, int] = {}

    def fallback_for(kind: CategoryKindStr) -> int:
        if kind not in fallback_ids:
            fallback_ids[kind] = fallback_category_id(session, user_id=user_id, kind=kind)
        return fallback_ids[kind]

    # Positive spend rows naming cashback, and the user's spend "Cashback" that
    # import_statement pre-filled them into (ADR-0013 rule 2) — looked up only if
    # there is such a row.
    cashback_row_ids = {
        r.id
        for r in rows
        if r.transaction_type == "spend"
        and r.amount_paise > 0
        and is_cashback_credit(r.merchant_raw or "")
    }
    cashback_id = (
        default_category_id(session, user_id=user_id, name=CASHBACK_CATEGORY_NAME, kind="spend")
        if cashback_row_ids
        else None
    )

    # Active (non-archived) category ids among the rows' current categories. A
    # spend row whose category was archived mid-review is then treated
    # like an untagged one below — so it never commits to a
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

    commit_row_ids = {r.id for r in rows}
    invalid_ids: set[int] = (
        (requested_ids - {r.id for r in fetched})
        | {r.id for r in fetched if r.confirmed_at is not None}
        | (commit_ids & discard_ids)
    )
    # Rows the server defaulted ("Other", or the Cashback prefill) — excluded from
    # pass-3 learning below, since a default isn't a merchant→category decision.
    defaulted_ids: set[int] = set()
    commits_as_transfer = card_bill_transfer_gate(session, user_id=user_id, batch=batch)
    for r in rows:
        if r.confirmed_at is not None:
            continue
        archived = r.category_id is not None and r.category_id not in active_category_ids
        # A spend row with no category, or one archived mid-review, defaults to
        # spend "Other". An income row only moves when its category was archived:
        # income may commit uncategorized, and auto_link (pass 2) moves a linked leg's
        # category to pre_link_category_id (ADR-0002 rule 9), so a defaulted income row would
        # save a category nobody chose.
        if r.category_id is None and commits_as_transfer(r):
            # ADR-0002 rules 7b, 7c: the bank leg of a bill whose card statement for this date is
            # in, or a card's bill-payment credit, committed uncategorised. A transfer from the
            # start, so pass 2 can still link it and an unlink leaves it out of spend and income.
            # No category, nothing learned.
            r.transaction_type = "transfer"
        elif r.transaction_type == "spend" and (r.category_id is None or archived):
            r.category_id = fallback_for("spend")
            defaulted_ids.add(r.id)
        elif r.transaction_type == "income" and archived:
            r.category_id = fallback_for("income")
            defaulted_ids.add(r.id)
        elif r.id in cashback_row_ids and r.category_id == cashback_id:
            # Still in the keyword prefill import_statement gave it: a server
            # default, not a merchant decision, so it does not teach (ADR-0004
            # rule 5). A user who moved it elsewhere made a real choice.
            defaulted_ids.add(r.id)

    if invalid_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "some transactions are not eligible to commit or discard",
                "invalid_ids": sorted(invalid_ids),
            },
        )

    # The shared delete (ADR-0002 rule 5): it releases any row pointing at a discarded one,
    # and its flush also writes the defaulted categories above. It is ORM-enabled, so the
    # discarded objects leave the session, and nothing below touches them.
    delete_owned_transactions(session, user_id=user_id, ids={r.id for r in discard_rows})

    now = clock.naive_utcnow()
    # Pass 1: stamp every confirmed_at and flush to the parent transaction.
    # Without this explicit flush, pass-2's first savepoint would flush
    # pending UPDATEs inside its own savepoint scope (per the F2 docstring's
    # SAVEPOINT-entry-flush analysis), coupling each row's DB state to the
    # savepoint's success.
    for txn in rows:
        txn.confirmed_at = now
    session.flush()

    # Pass 2: F4a-1 CC-bill auto-link, for card payments and bank debits alike (whichever
    # statement commits second links to the confirmed leg already on the board, ADR-0002
    # rule 7). Per-row savepoint inside the service.
    # Must run BEFORE pass 3 so any transaction_type flip lands before
    # learn_merchant_memory's should_learn_tag eligibility check.
    batch_acct = session.get(Account, batch.account_id) if batch.account_id is not None else None
    if batch_acct is not None:
        if batch_acct.type == "credit_card":
            for txn in rows:
                if (
                    txn.amount_paise > 0
                    and txn.transaction_type in ("income", "transfer")
                    and is_cc_payment(txn.merchant_normalized)
                ):
                    auto_link_cc_bill(session, user_id=user_id, txn=txn)
        elif batch_acct.type == "bank":
            has_child_cards = (
                session.scalar(
                    select(Account.id)
                    .where(
                        Account.user_id == user_id,
                        Account.parent_account_id == batch_acct.id,
                        Account.archived_at.is_(None),
                    )
                    .limit(1)
                )
                is not None
            )
            if has_child_cards:
                for txn in rows:
                    if txn.amount_paise < 0 and txn.transaction_type in ("spend", "transfer"):
                        auto_link_cc_bill(session, user_id=user_id, txn=txn)

    # Pass 3: emit the F3 (category) + F3a (label) learning signal per row via
    # the shared ``learn_merchant_memory`` — the SAME helper POST/PATCH and the
    # seeder use, so commit can't silently diverge from the learning contract.
    # One SELECT prefetches the committed rows' labels (avoids an N-query loop),
    # then one pass per row:
    #   * category — skipped for ``defaulted_ids`` (a server default isn't a
    #     merchant→category decision worth teaching), passed through otherwise;
    #   * labels — never excluded by ``defaulted_ids`` (a defaulted row can still
    #     carry legitimate user labels).
    # ``learn_merchant_memory`` applies the shared ``should_learn_tag`` gate once,
    # so rows pass-2 flipped to ``transfer`` (auto-linked CC payments) learn
    # neither — the gate reads the post-flip ``transaction_type``. Each
    # record_tag/record_label is one upsert statement on the parent transaction,
    # so no inter-write flush is needed here.
    labels_by_txn: dict[int, list[int]] = {}
    for txn_id, label_id in session.execute(
        select(TransactionLabel.transaction_id, TransactionLabel.label_id).where(
            TransactionLabel.transaction_id.in_(commit_row_ids),
            # user_id scope is defense-in-depth (commit_row_ids is already
            # user+batch-scoped and the composite FK guarantees ownership) —
            # parity with every other user-scoped query in this router.
            TransactionLabel.user_id == user_id,
        )
    ):
        labels_by_txn.setdefault(txn_id, []).append(label_id)
    resolver = load_alias_resolver(session, user_id=user_id)
    for txn in rows:
        learn_merchant_memory(
            session,
            user_id=user_id,
            resolver=resolver,
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


@router.get("/{batch_id}/reconciliation", response_model=BatchReconciliation)
def get_batch_reconciliation(
    batch_id: int,
    session: SessionDep,
    user_id: CurrentUserId,
) -> BatchReconciliation:
    """Recompute + persist this batch's statement-balance reconciliation.

    Recomputes via :func:`reconcile_batch` on every call rather than only
    reading the stored column — a commit or a discard of a pending row since
    the last check can flip a stale mismatch to matched (or vice versa).
    Persists the fresh delta and commits before returning, so
    ``GET /imports/pending`` picks up the same figure without a second
    reconciliation pass.

    ``expected_paise`` / ``actual_paise`` are derived algebraically from the
    delta (``expected = closing − opening``, ``actual = expected + delta``)
    rather than a second query — they cannot drift from the persisted delta
    by construction. ``None`` when the delta itself is ``None`` (no usable
    statement metadata, or an account-less batch), in which case ``status``
    is ``"unavailable"``.

    ``rows_removed_since_import`` (:func:`rows_removed_since_import`) is
    computed regardless of ``status`` — it's a live COUNT independent of
    whether the balance check itself could run — so a mismatch caused by a
    routine discard (an investment-transfer SIP debit, most commonly) reads
    as an explained qualifier rather than a bare, unexplained delta.

    Route ordering: declared **after** ``/pending`` — that literal path must
    never be captured as a ``batch_id`` path param.
    """
    batch = _get_batch_or_404(session, batch_id=batch_id, user_id=user_id)

    delta = reconcile_batch(session, user_id=user_id, batch=batch)
    batch.reconciliation_delta_paise = delta
    # Independent of the balance check above — a live COUNT, computed and
    # returned regardless of `status`, so it pairs with a mismatch to explain
    # a discard-noise false positive rather than leaving it unexplained.
    removed = rows_removed_since_import(session, batch=batch)

    opening = batch.statement_opening_balance_paise
    closing = batch.statement_closing_balance_paise
    expected_paise: int | None = None
    actual_paise: int | None = None
    if delta is not None and opening is not None and closing is not None:
        expected_paise = closing - opening
        actual_paise = expected_paise + delta

    recon_status: Literal["unavailable", "matched", "mismatched"]
    if delta is None:
        recon_status = "unavailable"
    elif delta == 0:
        recon_status = "matched"
    else:
        recon_status = "mismatched"

    session.commit()

    return BatchReconciliation(
        batch_id=batch.id,
        opening_balance_paise=opening,
        closing_balance_paise=closing,
        period_start=batch.period_start,
        period_end=batch.period_end,
        expected_paise=expected_paise,
        actual_paise=actual_paise,
        delta_paise=delta,
        status=recon_status,
        rows_removed_since_import=removed,
    )


@router.get("/samples", response_model=list[SampleStatementRead])
def list_sample_statements(
    session: SessionDep, user_id: CurrentUserId
) -> list[SampleStatementRead]:
    """The bundled sample statements, each with the caller's accounts it can be imported
    into (ADR-0003 rule 15). Open to guests, unlike ``POST /imports``."""
    return [
        SampleStatementRead(
            id=sample.id,
            label=sample.label,
            issuer=sample.issuer,
            account_type=sample.account_type,
            account_ids=eligible_account_ids(session, user_id=user_id, sample=sample),
        )
        for sample in SAMPLES
    ]


@router.post("/samples", response_model=ImportSummary)
def import_sample(
    payload: SampleImportCreate, session: SessionDep, user_id: CurrentUserId
) -> ImportSummary:
    """Stage a bundled sample statement into one of the caller's accounts (ADR-0003 rule 15).

    Open to guests. Generic detail strings, as ``POST /imports``. A bundled sample that fails
    to parse is a shipped bug, not a client error, so ``ParserError`` is left unmapped (500).
    """
    try:
        result = import_sample_statement(
            session, user_id=user_id, account_id=payload.account_id, sample_id=payload.sample_id
        )
    except UnknownSampleError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="unknown sample statement",
        ) from e
    except AccountNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="account not found or archived",
        ) from e
    except NonInrAccountError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="statement import requires an INR account",
        ) from e
    except SampleAccountMismatchError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="this sample is for a different bank or account type",
        ) from e
    except StatementMerchantTooLongError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)) from e
    session.commit()
    return ImportSummary.model_validate(result)
