"""``/api/v1/backup`` — download a spend backup zip + additively import one (PRD §F10).

* ``GET /backup`` — stream ``fin-tracker-backup-YYYYMMDD-HHMMSS.zip`` (confirmed spend
  transactions + the accounts/categories they reference). Built by
  :func:`app.services.export_service.build_backup_zip`.
* ``POST /backup/import`` — upload a backup zip; additively load it (dedup by recomputed
  fingerprint, never wipes existing data). Thin route: validate upload size, delegate to
  :func:`app.services.backup_import_service.import_backup_zip`, commit, return the summary.
  A malformed/unreadable zip maps to a **generic** 422 (no cell contents / exception args in
  the body), same input-hygiene posture as ``/imports``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Response, UploadFile, status

from app.api.deps import CurrentUserId, SessionDep
from app.core import clock
from app.parsers.base import ParserError
from app.schemas import BackupImportSummary
from app.services.backup_import_service import import_backup_zip
from app.services.export_service import build_backup_zip

# A personal spend history zips to well under this; the cap defends the worker
# against an accidental large upload and is not a security boundary.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

router = APIRouter(prefix="/backup", tags=["backup"])


@router.get("")
def download_backup(session: SessionDep, user_id: CurrentUserId) -> Response:
    """Stream the spend backup zip as an attachment."""
    data = build_backup_zip(session, user_id=user_id)
    filename = f"fin-tracker-backup-{clock.utcnow():%Y%m%d-%H%M%S}.zip"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import", response_model=BackupImportSummary)
def import_backup(
    session: SessionDep,
    user_id: CurrentUserId,
    file: Annotated[UploadFile, File()],
) -> BackupImportSummary:
    """Additively import a backup zip. Non-destructive: dedups by recomputed fingerprint."""
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="file too large",
        )
    file_bytes = file.file.read()
    try:
        result = import_backup_zip(session, user_id=user_id, file_bytes=file_bytes)
    except ParserError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="could not parse backup file",
        ) from e
    session.commit()
    return BackupImportSummary.model_validate(result)
