"""End-to-end tests for ``/api/v1/labels`` (PRD §F3a — user tags).

Covers the HTTP surface: name normalization, 409 dup mapping, ``extra="forbid"``,
empty-body / same-name PATCH no-ops, cross-user isolation (404 / omit), and the
hard-delete cascade that clears ``transaction_labels`` links.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Account, Label, Transaction, TransactionLabel, User


def _post(client: TestClient, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    resp = client.post("/api/v1/labels", json=payload)
    return resp.status_code, resp.json()


def _patch(
    client: TestClient, label_id: int, payload: dict[str, object]
) -> tuple[int, dict[str, object]]:
    resp = client.patch(f"/api/v1/labels/{label_id}", json=payload)
    return resp.status_code, resp.json()


def _make_label(session: Session, user_id, name: str) -> Label:  # noqa: ANN001
    label = Label(user_id=user_id, name=name)
    session.add(label)
    session.commit()
    session.refresh(label)
    return label


# --------------------------------------------------------------- GET /labels
def test_list_empty(client: TestClient, seeded_user: User) -> None:
    resp = client.get("/api/v1/labels")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_sorted_and_shape(client: TestClient, seeded_user: User, session: Session) -> None:
    _make_label(session, seeded_user.id, "travel")
    _make_label(session, seeded_user.id, "food")
    resp = client.get("/api/v1/labels")
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["name"] for r in rows] == ["food", "travel"]  # name ASC
    assert set(rows[0].keys()) == {"id", "name"}
    assert "user_id" not in rows[0]


def test_list_omits_foreign_user(client: TestClient, seeded_user: User, session: Session) -> None:
    other = User(id=uuid4())
    session.add(other)
    session.flush()
    _make_label(session, other.id, "theirs")
    _make_label(session, seeded_user.id, "mine")
    resp = client.get("/api/v1/labels")
    assert {r["name"] for r in resp.json()} == {"mine"}


# --------------------------------------------------------------- POST /labels
def test_create_happy(
    client: TestClient, seeded_user: User, session_factory: sessionmaker[Session]
) -> None:
    status_code, body = _post(client, {"name": "travel"})
    assert status_code == 201
    assert body["name"] == "travel"
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Label)) == 1


def test_create_strips_hash_and_lowercases(client: TestClient, seeded_user: User) -> None:
    status_code, body = _post(client, {"name": "#Travel"})
    assert status_code == 201
    assert body["name"] == "travel"


def test_create_collapses_whitespace(client: TestClient, seeded_user: User) -> None:
    status_code, body = _post(client, {"name": "  Food   Court  "})
    assert status_code == 201
    assert body["name"] == "food court"


def test_create_strips_semicolon(client: TestClient, seeded_user: User) -> None:
    status_code, body = _post(client, {"name": "food;travel"})
    assert status_code == 201
    assert body["name"] == "foodtravel"


def test_create_blank_after_normalize_422(client: TestClient, seeded_user: User) -> None:
    status_code, body = _post(client, {"name": "#"})
    assert status_code == 422
    assert "name must not be blank" in str(body)


def test_create_empty_name_422(client: TestClient, seeded_user: User) -> None:
    status_code, _ = _post(client, {"name": ""})
    assert status_code == 422


def test_create_too_long_422(client: TestClient, seeded_user: User) -> None:
    status_code, _ = _post(client, {"name": "a" * 65})
    assert status_code == 422


def test_create_rejects_extra_fields(client: TestClient, seeded_user: User) -> None:
    status_code, _ = _post(client, {"name": "travel", "color": "#fff"})
    assert status_code == 422


def test_create_duplicate_409(client: TestClient, seeded_user: User) -> None:
    first_status, _ = _post(client, {"name": "travel"})
    # Same normalized name via a different surface form → still a dup.
    second_status, second_body = _post(client, {"name": "#Travel"})
    assert first_status == 201
    assert second_status == 409
    assert second_body["detail"] == "label already exists"


# --------------------------------------------------------------- PATCH /labels/{id}
def test_patch_rename_happy(client: TestClient, seeded_user: User, session: Session) -> None:
    label = _make_label(session, seeded_user.id, "trvl")
    status_code, body = _patch(client, label.id, {"name": "travel"})
    assert status_code == 200
    assert body["name"] == "travel"


def test_patch_rename_to_same_normalized_noop(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    label = _make_label(session, seeded_user.id, "travel")
    status_code, body = _patch(client, label.id, {"name": "#Travel"})
    assert status_code == 200
    assert body["name"] == "travel"


def test_patch_empty_body_noop(client: TestClient, seeded_user: User, session: Session) -> None:
    label = _make_label(session, seeded_user.id, "travel")
    status_code, body = _patch(client, label.id, {})
    assert status_code == 200
    assert body["name"] == "travel"


def test_patch_collision_409(client: TestClient, seeded_user: User, session: Session) -> None:
    _make_label(session, seeded_user.id, "food")
    b = _make_label(session, seeded_user.id, "travel")
    status_code, body = _patch(client, b.id, {"name": "food"})
    assert status_code == 409
    assert body["detail"] == "label already exists"


def test_patch_null_name_422(client: TestClient, seeded_user: User, session: Session) -> None:
    label = _make_label(session, seeded_user.id, "travel")
    resp = client.patch(f"/api/v1/labels/{label.id}", json={"name": None})
    assert resp.status_code == 422
    assert "name cannot be cleared" in resp.text


def test_patch_unknown_404(client: TestClient, seeded_user: User) -> None:
    status_code, body = _patch(client, 9999, {"name": "x"})
    assert status_code == 404
    assert body["detail"] == "label not found"


def test_patch_foreign_user_404(
    client: TestClient, seeded_user: User, session: Session, session_factory: sessionmaker[Session]
) -> None:
    other = User(id=uuid4())
    session.add(other)
    session.flush()
    foreign = _make_label(session, other.id, "theirs")
    status_code, body = _patch(client, foreign.id, {"name": "hijacked"})
    assert status_code == 404
    with session_factory() as s:
        assert s.get(Label, foreign.id).name == "theirs"


# --------------------------------------------------------------- DELETE /labels/{id}
def test_delete_happy(
    client: TestClient, seeded_user: User, session: Session, session_factory: sessionmaker[Session]
) -> None:
    label = _make_label(session, seeded_user.id, "travel")
    resp = client.delete(f"/api/v1/labels/{label.id}")
    assert resp.status_code == 204
    assert resp.text == ""
    with session_factory() as s:
        assert s.get(Label, label.id) is None


def test_delete_unknown_404(client: TestClient, seeded_user: User) -> None:
    resp = client.delete("/api/v1/labels/9999")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "label not found"


def test_delete_foreign_user_404(
    client: TestClient, seeded_user: User, session: Session, session_factory: sessionmaker[Session]
) -> None:
    other = User(id=uuid4())
    session.add(other)
    session.flush()
    foreign = _make_label(session, other.id, "theirs")
    resp = client.delete(f"/api/v1/labels/{foreign.id}")
    assert resp.status_code == 404
    with session_factory() as s:
        assert s.get(Label, foreign.id) is not None


def test_delete_cascades_transaction_links(
    client: TestClient,
    seeded_user: User,
    axis_account: Account,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Hard-deleting a label removes its transaction_labels links (ON DELETE
    CASCADE), but leaves the transaction itself intact."""
    label = _make_label(session, seeded_user.id, "travel")
    txn = Transaction(
        user_id=seeded_user.id,
        account_id=axis_account.id,
        date=date(2026, 3, 5),
        amount_paise=-8500,
        transaction_type="spend",
        merchant_raw="INDIGO",
        merchant_normalized="indigo",
        fingerprint="bb" * 32,
        source="manual",
    )
    session.add(txn)
    session.commit()
    session.add(TransactionLabel(transaction_id=txn.id, label_id=label.id, user_id=seeded_user.id))
    session.commit()

    resp = client.delete(f"/api/v1/labels/{label.id}")
    assert resp.status_code == 204

    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(TransactionLabel)) == 0
        assert s.get(Transaction, txn.id) is not None  # transaction survives
