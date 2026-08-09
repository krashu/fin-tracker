"""Real-PDF end-to-end test for ``POST /api/v1/imports`` (PRD §F1).

Exercises pikepdf decrypt + pdfplumber extract + interpret_tables +
import_service + DB persist against the user's actual Axis statements
at ``backend/tests/fixtures/_local/axis_cc*.pdf``.

Skips when no PDFs are present or ``AXIS_TEST_PWD`` is unset; gated by
the ``real_pdf`` marker so CI / sandboxes can opt out with
``pytest -m "not real_pdf"``.

PII discipline: assertion messages NEVER echo ``resp.text``, merchant
strings, or any other PDF-derived content. Failure investigation is via
the DB, not the assertion banner.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Account, ImportBatch, Transaction

pytestmark = pytest.mark.real_pdf


def _post(
    client: TestClient, *, account_id: int, file_bytes: bytes, password: str
) -> dict[str, object]:
    resp = client.post(
        "/api/v1/imports",
        data={"account_id": str(account_id), "password": password},
        files={"file": ("statement.pdf", file_bytes, "application/pdf")},
    )
    assert resp.status_code == 200, f"status={resp.status_code}"
    return resp.json()


def test_axis_real_pdf_imports_end_to_end(
    client: TestClient,
    axis_account: Account,
    axis_real_pdf: bytes,
    axis_real_password: str,
    session_factory: sessionmaker[Session],
) -> None:
    body = _post(
        client,
        account_id=axis_account.id,
        file_bytes=axis_real_pdf,
        password=axis_real_password,
    )
    assert body["imported"] > 0, "parser found zero transactions in PDF"
    assert body["skipped"] == 0
    assert body["already_imported"] is False

    with session_factory() as s:
        batch = s.get(ImportBatch, body["batch_id"])
        assert batch is not None
        assert batch.status == "completed"
        assert batch.imported_count == body["imported"]

        txn_count = s.scalar(select(func.count()).select_from(Transaction))
        assert txn_count == body["imported"]

        empty_merchants = s.scalar(
            select(func.count()).select_from(Transaction).where(Transaction.merchant_raw == "")
        )
        assert empty_merchants == 0, "parser produced empty merchant_raw on some rows"


def test_axis_real_pdf_reimport_reconciles_no_new_rows(
    client: TestClient,
    axis_account: Account,
    axis_real_pdf: bytes,
    axis_real_password: str,
    session_factory: sessionmaker[Session],
) -> None:
    """Re-upload (with password) re-parses and skips every already-present row."""
    first = _post(
        client,
        account_id=axis_account.id,
        file_bytes=axis_real_pdf,
        password=axis_real_password,
    )
    second = _post(
        client,
        account_id=axis_account.id,
        file_bytes=axis_real_pdf,
        password=axis_real_password,
    )

    assert second["already_imported"] is True
    assert second["batch_id"] == first["batch_id"]
    assert second["imported"] == 0
    # Every row is now present (pending from the first import) → all skipped.
    assert second["skipped"] == first["imported"]
    assert second["pending_count"] == first["imported"]

    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(ImportBatch)) == 1
        assert s.scalar(select(func.count()).select_from(Transaction)) == first["imported"]
