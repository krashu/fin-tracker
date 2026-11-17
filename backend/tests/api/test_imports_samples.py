"""``GET`` / ``POST /api/v1/imports/samples`` (ADR-0003 rule 15)."""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Account, ImportBatch, User
from app.parsers import ParsedStatement
from app.services import sample_statements

SAMPLE_ID = "axis-cc-2025-06"


def _card(session: Session, user: User, **kw: object) -> Account:
    fields: dict[str, object] = {
        "name": "Axis CC",
        "type": "credit_card",
        "issuer": "axis",
        "last4": "1234",
    }
    fields.update(kw)
    account = Account(user_id=user.id, **fields)
    session.add(account)
    session.commit()
    return account


def test_list_samples_shape(client: TestClient, session: Session, seeded_user: User) -> None:
    card = _card(session, seeded_user)

    resp = client.get("/api/v1/imports/samples")

    assert resp.status_code == 200, resp.text
    [sample] = [s for s in resp.json() if s["id"] == SAMPLE_ID]
    assert set(sample) == {"id", "label", "issuer", "account_type", "account_ids"}
    assert sample["id"] == SAMPLE_ID
    assert sample["account_ids"] == [card.id]


def test_list_samples_offers_only_matching_accounts(
    client: TestClient, session: Session, seeded_user: User
) -> None:
    live = _card(session, seeded_user, name="Live", last4="1111")
    _card(session, seeded_user, name="Bank", type="bank", last4="2222")
    archived = _card(session, seeded_user, name="Old", last4="3333")
    assert client.delete(f"/api/v1/accounts/{archived.id}").status_code == 204

    [sample] = [s for s in client.get("/api/v1/imports/samples").json() if s["id"] == SAMPLE_ID]

    assert sample["account_ids"] == [live.id]


def test_sample_into_a_mismatched_account_422(
    client: TestClient, session: Session, seeded_user: User
) -> None:
    bank = _card(session, seeded_user, type="bank", issuer="hdfc")

    resp = client.post(
        "/api/v1/imports/samples", json={"sample_id": SAMPLE_ID, "account_id": bank.id}
    )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "this sample is for a different bank or account type"
    assert session.scalar(select(func.count()).select_from(ImportBatch)) == 0


def test_unknown_sample_422(client: TestClient, session: Session, seeded_user: User) -> None:
    card = _card(session, seeded_user)

    resp = client.post("/api/v1/imports/samples", json={"sample_id": "nope", "account_id": card.id})

    assert resp.status_code == 422
    assert resp.json()["detail"] == "unknown sample statement"


def test_sample_into_an_archived_account_422(
    client: TestClient, session: Session, seeded_user: User
) -> None:
    card = _card(session, seeded_user)
    assert client.delete(f"/api/v1/accounts/{card.id}").status_code == 204

    resp = client.post(
        "/api/v1/imports/samples", json={"sample_id": SAMPLE_ID, "account_id": card.id}
    )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "account not found or archived"


def test_sample_with_a_too_long_merchant_422(
    client: TestClient, session: Session, seeded_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0006 rule 3b on the sample route too: a too-long row is a 422 naming its
    position, not a 500, and nothing is staged."""
    card = _card(session, seeded_user)
    real_load = sample_statements.load_sample

    def load_with_a_long_row(sample: sample_statements.SampleStatement):
        statement, file_hash = real_load(sample)
        rows = [dataclasses.replace(statement.rows[0], merchant_raw="a" * 513), *statement.rows[1:]]
        return ParsedStatement(rows=rows, summary=statement.summary), file_hash

    monkeypatch.setattr(sample_statements, "load_sample", load_with_a_long_row)

    resp = client.post(
        "/api/v1/imports/samples", json={"sample_id": SAMPLE_ID, "account_id": card.id}
    )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "merchant on statement row 1 is longer than 512 characters"
    assert client.get("/api/v1/imports/pending").json() == []
    assert session.scalar(select(func.count()).select_from(ImportBatch)) == 0
