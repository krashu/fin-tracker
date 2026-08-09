"""Import batch — one row per uploaded statement / CAS file.

Captures the parse → dedup → tag → persist pipeline outcome for one upload
so the user can see "10 imported, 3 skipped as duplicate" on the review
screen and so a re-upload of the same file (matched by
``source_file_hash``) can be detected upstream of row-level fingerprint
dedup.

``account_id`` is nullable: spend-statement batches (PRD §F1) are scoped to one
account, but investment-import batches (PRD §F7 / ``investment_import_service``) are
account-less — investments are decoupled from the spend tables. An investment batch is
identified by ``account_id IS NULL`` + ``parser_name == "investment_csv"``.

``status`` is informational — ``failed`` lets future error-reporting UIs
list batches that need user attention without scanning import logs. The
investment importer reuses ``failed`` for a batch left with FX-unavailable
rows (some rows couldn't be stamped — re-upload after ``POST /fx/refresh``
reprocesses); ``imported_count`` still records how many landed.
"""

from __future__ import annotations

import uuid
from typing import Literal, get_args

from sqlalchemy import Enum, ForeignKey, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

ImportStatusStr = Literal["pending", "completed", "failed"]


class ImportBatch(Base, TimestampMixin):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    # Nullable: spend batches carry an account; backup-restore and investment batches
    # are account-less (they resolve an account per row instead, or have none at all).
    # A PLAIN FK, not ADR-0002's composite same-user one, so reads that join Account
    # off this column must restate `Account.user_id` themselves — see
    # `GET /imports/pending`.
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    source_file_hash: Mapped[str] = mapped_column(String(64), index=True)
    parser_name: Mapped[str] = mapped_column(String(64))
    imported_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    skipped_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    status: Mapped[ImportStatusStr] = mapped_column(
        Enum(
            *get_args(ImportStatusStr),
            name="import_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        ),
        default="pending",
        server_default="pending",
    )
    error_message: Mapped[str | None] = mapped_column(String(1024))
