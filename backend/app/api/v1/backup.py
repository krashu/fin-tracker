"""``/api/v1/backup`` — download a spend backup zip + additively import one (PRD §F10).

* ``GET /backup`` — stream ``fin-tracker-backup-YYYYMMDD-HHMMSS.zip`` (confirmed spend
  transactions + the accounts/categories they reference). Built by
  :func:`app.services.export_service.build_backup_zip`.
* ``POST /backup/import`` — upload a backup zip; additively load it (dedup by recomputed
  fingerprint or recomputed provenance, never wipes existing data). Thin route: read the body
  in memory under the size cap (``app/api/uploads.py``), delegate to
  :func:`app.services.backup_import_service.import_backup_zip`, commit, return the summary.
  A malformed/unreadable zip maps to a **generic** 422 (no cell contents / exception args in
  the body), same input-hygiene posture as ``/imports``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict

from app.api.deps import CurrentUserId, NonGuestUserId, SessionDep
from app.api.uploads import read_upload
from app.core import clock
from app.parsers.backup_csv import BACKUP_FORMAT_VERSION, BackupFormatVersionError
from app.parsers.base import ParserError
from app.schemas import BackupImportSummary
from app.services.backup_import_service import import_backup_zip
from app.services.export_service import build_backup_zip

# A personal spend history zips to well under this; the cap defends the worker
# against an accidental large upload and is not a security boundary.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

_BACKUP_UPLOAD_BODY = {
    "required": True,
    "content": {
        "multipart/form-data": {
            "schema": {
                "type": "object",
                "required": ["file"],
                "properties": {"file": {"type": "string", "format": "binary"}},
            }
        }
    },
}


class _BackupUploadForm(BaseModel):
    """``POST /backup/import`` has no non-file field."""

    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True, slots=True)
class BackupUpload:
    file_bytes: bytes = field(repr=False)


async def read_backup_upload(request: Request) -> BackupUpload:
    _, file_bytes = await read_upload(
        request, _BackupUploadForm, max_bytes=MAX_UPLOAD_BYTES, max_fields=0
    )
    return BackupUpload(file_bytes=file_bytes)


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


@router.post(
    "/import",
    response_model=BackupImportSummary,
    openapi_extra={"requestBody": _BACKUP_UPLOAD_BODY},
    responses={413: {"description": "Request body over the upload cap"}},
)
def import_backup(
    session: SessionDep,
    user_id: NonGuestUserId,
    upload: Annotated[BackupUpload, Depends(read_backup_upload)],
) -> BackupImportSummary:
    """Additively import a backup zip. Non-destructive: dedups by recomputed fingerprint or
    recomputed provenance."""
    try:
        result = import_backup_zip(session, user_id=user_id, file_bytes=upload.file_bytes)
    except BackupFormatVersionError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unsupported backup format version (expected {BACKUP_FORMAT_VERSION}); "
            "export a new backup",
        ) from e
    except ParserError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="could not parse backup file",
        ) from e
    session.commit()
    return BackupImportSummary.model_validate(result)
