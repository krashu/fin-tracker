"""Backup response schema (PRD §F10).

:class:`BackupImportSummary` — response body of ``POST /api/v1/backup/import``. Additive,
deduped import: ``txns_skipped_dupe`` counts rows already present (matched by recomputed
fingerprint), ``rows_rejected`` counts rows dropped across parse + resolution, and
``transfers_relinked`` counts transfer legs re-paired. ``warnings`` are PII-safe (``"<file>
row N: <reason>"`` — line number + cause only, never raw cell contents).

``GET /api/v1/backup`` streams a zip (``application/zip``) and so has no JSON schema.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class BackupImportSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    batch_id: int
    accounts_new: int
    accounts_matched: int
    categories_new: int
    categories_matched: int
    txns_imported: int
    txns_skipped_dupe: int
    rows_rejected: int
    transfers_relinked: int
    warnings: list[str]
