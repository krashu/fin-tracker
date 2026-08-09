"""Import-flow request/response schemas (per PRD §F1 import + review).

* :class:`ImportSummary` — response body of ``POST /api/v1/imports``.
* :class:`ImportCommit` — request body of
  ``POST /api/v1/imports/{batch_id}/commit``. Id-list only by design;
  any category edits the user makes in the review queue must be PATCHed
  to ``/transactions/{id}`` before commit. Commit reads ``category_id``
  from the DB, not the request, so the route stays a pure lifecycle
  transition (no edit + commit mixing).
* :class:`InvestmentCsvImportSummary` — response body of
  ``POST /api/v1/imports/investments`` (investment-transaction CSV, PRD §F7).
  Account-less and commits directly (no review queue — investments have no
  categories); the summary carries the counts plus PII-safe per-row reject
  warnings (line number + reason, never cell contents).

``POST /imports`` + ``/imports/investments`` upload fields are multipart
``Form()`` / ``File()`` parameters in the route, not a JSON body — no create schema.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ImportSummary(BaseModel):
    """Response body of ``POST /api/v1/imports``.

    ``already_imported`` means a completed batch for this file hash already
    existed (the file was seen before) — not that the upload was a no-op. A
    re-upload re-parses and reconciles, re-staging rows missing from the DB.
    ``pending_count`` is the batch's rows still awaiting review after the import;
    the frontend routes on it (>0 → review queue, 0 → "all already in expenses").

    ``duplicate_of_account_id`` is *an* other account of the same user that this
    byte-identical file was already imported into (a wrong-account mis-import — the
    per-account F4 dedup scope cannot catch it). It is an id and never a name, and
    ``duplicate_of_account_archived`` says whether that account is archived, because
    ``GET /accounts`` filters archived rows out and the id would otherwise be
    unresolvable to a label.
    """

    model_config = ConfigDict(from_attributes=True)

    batch_id: int
    imported: int
    skipped: int
    already_imported: bool
    pending_count: int
    duplicate_of_account_id: int | None = None
    duplicate_of_account_archived: bool = False


class ImportCommit(BaseModel):
    """Request body for ``POST /api/v1/imports/{batch_id}/commit``.

    ``min_length=1`` because empty-list commits are a frontend bug, not a
    valid no-op (the cancel route exists for "do nothing").
    """

    model_config = ConfigDict(extra="forbid")

    transaction_ids: list[int] = Field(min_length=1)


class PendingImportBatch(BaseModel):
    """One open import batch surfaced by ``GET /api/v1/imports/pending``.

    A batch is "pending" while it still has ≥1 transaction with
    ``confirmed_at IS NULL`` (i.e. rows sitting in the review queue).
    ``pending_count`` is that count. ``account_name`` / ``account_last4``
    label the batch for the notification-bell dropdown; both are nullable, and
    genuinely so — **backup restore** builds an account-less batch
    (``backup_import_service`` passes ``account_id=None`` while resolving a real
    account per row) and restores each row with the CSV's ``confirmed_at``, so an
    unconfirmed restore appears here with rows to count and no account to name it.
    (Investment batches are also account-less but commit directly, so they never
    reach this feed; the CAS importer that this note used to cite no longer exists.)

    ``last4`` flows to the client via ``AccountRead`` too — but that is user-scoped,
    which is exactly what a join on a plain ``account_id`` FK is not, so the read
    restates ``Account.user_id`` in its ON clause. A foreign account yields a null
    label rather than another user's name and last4.
    """

    batch_id: int
    account_name: str | None
    account_last4: str | None
    pending_count: int


class InvestmentCsvImportSummary(BaseModel):
    """Response body of ``POST /api/v1/imports/investments``.

    ``already_imported`` short-circuits an identical re-upload (matched by
    ``source_file_hash``) — the client shows "already imported — nothing new".
    ``warnings`` are PII-safe (line number + reason; never raw cells / folio / PAN).
    """

    model_config = ConfigDict(from_attributes=True)

    batch_id: int
    instruments_new: int
    txns_imported: int
    txns_skipped_dupe: int
    rows_rejected: int
    already_imported: bool
    warnings: list[str]
