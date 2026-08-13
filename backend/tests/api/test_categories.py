"""End-to-end tests for ``/api/v1/categories`` (PRD §F5).

Constraint-layer invariants (per-user uniqueness, archive-then-recreate,
unarchive-into-existing-active raises) are already locked in by the
unit tests in :mod:`tests.models.test_models`. These tests focus on the
HTTP translation: 409 status mapping, ``extra="forbid"`` rejection,
empty-body PATCH semantics, idempotent DELETE via the archived filter,
and the DELETE keeping ``merchant_tag_map`` rows (including user-authored
``pinned`` ones) while the archived-category filter hides them from F3
prefill and the rules list.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Category, MerchantTagMap, Transaction, User


def _post(client: TestClient, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    resp = client.post("/api/v1/categories", json=payload)
    return resp.status_code, resp.json()


# ---------------------------------------------------------------------------
# GET /categories
# ---------------------------------------------------------------------------


def test_list_empty(client: TestClient, seeded_user: User) -> None:
    resp = client.get("/api/v1/categories")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_returns_15_seeded_sorted(
    client: TestClient,
    seeded_categories: list[Category],
) -> None:
    resp = client.get("/api/v1/categories")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 15
    names = [row["name"] for row in body]
    assert names == sorted(names)


def test_list_omits_archived(
    client: TestClient,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    food = next(c for c in seeded_categories if c.name == "Food")
    food.archived_at = datetime.now(UTC)
    session.commit()

    resp = client.get("/api/v1/categories")
    assert resp.status_code == 200
    names = {row["name"] for row in resp.json()}
    assert "Food" not in names
    assert len(names) == 14


def test_list_omits_foreign_user(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    other = User(id=uuid4())
    session.add(other)
    session.flush()
    session.add(Category(user_id=other.id, name="NotMine", is_seeded=False))
    session.add(Category(user_id=seeded_user.id, name="Mine", is_seeded=False))
    session.commit()

    resp = client.get("/api/v1/categories")
    assert resp.status_code == 200
    names = {row["name"] for row in resp.json()}
    assert names == {"Mine"}


def test_list_response_shape(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    session.add(Category(user_id=seeded_user.id, name="Coffee", is_seeded=False))
    session.commit()

    resp = client.get("/api/v1/categories")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert set(rows[0].keys()) == {
        "id",
        "name",
        "kind",
        "is_seeded",
        "archived_at",
        "color",
        "parent_id",
    }
    assert "user_id" not in rows[0]
    # Derived-color default: a category created without a color reads back NULL.
    assert rows[0]["color"] is None
    assert rows[0]["parent_id"] is None


# ---------------------------------------------------------------------------
# POST /categories
# ---------------------------------------------------------------------------


def test_create_happy(
    client: TestClient,
    seeded_user: User,
    session_factory: sessionmaker[Session],
) -> None:
    status_code, body = _post(client, {"name": "Coffee"})
    assert status_code == 201
    assert body["name"] == "Coffee"
    assert body["is_seeded"] is False
    assert body["archived_at"] is None

    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Category)) == 1


def test_create_strips_whitespace(
    client: TestClient,
    seeded_user: User,
) -> None:
    status_code, body = _post(client, {"name": "  Coffee  "})
    assert status_code == 201
    assert body["name"] == "Coffee"


def test_create_blank_after_strip_422(
    client: TestClient,
    seeded_user: User,
) -> None:
    status_code, _ = _post(client, {"name": "   "})
    assert status_code == 422


def test_create_empty_name_422(
    client: TestClient,
    seeded_user: User,
) -> None:
    status_code, _ = _post(client, {"name": ""})
    assert status_code == 422


def test_create_too_long_name_422(
    client: TestClient,
    seeded_user: User,
) -> None:
    status_code, _ = _post(client, {"name": "a" * 65})
    assert status_code == 422


def test_create_rejects_extra_fields(
    client: TestClient,
    seeded_user: User,
) -> None:
    status_code, _ = _post(client, {"name": "Coffee", "is_seeded": True})
    assert status_code == 422


def test_create_duplicate_active_409(
    client: TestClient,
    seeded_user: User,
    session_factory: sessionmaker[Session],
) -> None:
    first_status, _ = _post(client, {"name": "Coffee"})
    second_status, second_body = _post(client, {"name": "Coffee"})
    assert first_status == 201
    assert second_status == 409
    assert second_body["detail"] == "category name already exists"

    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Category)) == 1


def test_create_against_archived_name_succeeds(
    client: TestClient,
    seeded_user: User,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    first_status, first_body = _post(client, {"name": "Coffee"})
    assert first_status == 201

    archived = session.get(Category, first_body["id"])
    assert archived is not None
    archived.archived_at = datetime.now(UTC)
    session.commit()

    second_status, _ = _post(client, {"name": "Coffee"})
    assert second_status == 201

    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Category)) == 2


def test_create_seeded_then_user_duplicate_409(
    client: TestClient,
    seeded_categories: list[Category],
) -> None:
    status_code, body = _post(client, {"name": "Food"})
    assert status_code == 409
    assert body["detail"] == "category name already exists"


def test_create_defaults_kind_spend(
    client: TestClient,
    seeded_user: User,
) -> None:
    """A {name}-only POST defaults kind to spend (back-compat wire shape)."""
    status_code, body = _post(client, {"name": "Coffee"})
    assert status_code == 201
    assert body["kind"] == "spend"


def test_create_with_kind_income(
    client: TestClient,
    seeded_user: User,
) -> None:
    status_code, body = _post(client, {"name": "Bonus", "kind": "income"})
    assert status_code == 201
    assert body["kind"] == "income"


def test_create_invalid_kind_422(
    client: TestClient,
    seeded_user: User,
) -> None:
    # transfer is a transaction_type but NOT a category kind — Literal-bound.
    status_code, _ = _post(client, {"name": "Bonus", "kind": "transfer"})
    assert status_code == 422


def test_create_with_color(
    client: TestClient,
    seeded_user: User,
) -> None:
    status_code, body = _post(client, {"name": "Coffee", "color": "#4f46e5"})
    assert status_code == 201
    assert body["color"] == "#4f46e5"


def test_create_color_normalized_lowercase(
    client: TestClient,
    seeded_user: User,
) -> None:
    status_code, body = _post(client, {"name": "Coffee", "color": "#4F46E5"})
    assert status_code == 201
    assert body["color"] == "#4f46e5"


def test_create_invalid_color_422(
    client: TestClient,
    seeded_user: User,
) -> None:
    # Not a #rrggbb hex — rejected at the Pydantic boundary.
    for bad in ("chart-3", "#fff", "#12345g", "blurple"):
        status_code, _ = _post(client, {"name": f"C{bad}", "color": bad})
        assert status_code == 422, bad


def test_create_same_name_different_kind_allowed(
    client: TestClient,
    seeded_user: User,
    session_factory: sessionmaker[Session],
) -> None:
    """The load-bearing new invariant: "Other" can exist once per kind."""
    spend_status, _ = _post(client, {"name": "Other", "kind": "spend"})
    income_status, _ = _post(client, {"name": "Other", "kind": "income"})
    assert spend_status == 201
    assert income_status == 201

    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Category)) == 2


def test_create_duplicate_same_name_same_kind_409(
    client: TestClient,
    seeded_user: User,
) -> None:
    first_status, _ = _post(client, {"name": "Bonus", "kind": "income"})
    second_status, second_body = _post(client, {"name": "Bonus", "kind": "income"})
    assert first_status == 201
    assert second_status == 409
    assert second_body["detail"] == "category name already exists"


# ---------------------------------------------------------------------------
# PATCH /categories/{id}
# ---------------------------------------------------------------------------


def _patch(
    client: TestClient,
    category_id: int,
    payload: dict[str, object],
) -> tuple[int, dict[str, object]]:
    resp = client.patch(f"/api/v1/categories/{category_id}", json=payload)
    return resp.status_code, resp.json()


def test_patch_rename_happy(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    cat = Category(user_id=seeded_user.id, name="Coffee", is_seeded=False)
    session.add(cat)
    session.commit()
    session.refresh(cat)

    status_code, body = _patch(client, cat.id, {"name": "Caffeine"})
    assert status_code == 200
    assert body["name"] == "Caffeine"


def test_patch_rename_seeded_allowed(
    client: TestClient,
    seeded_categories: list[Category],
) -> None:
    food = next(c for c in seeded_categories if c.name == "Food")
    status_code, body = _patch(client, food.id, {"name": "Food & Drink"})
    assert status_code == 200
    assert body["name"] == "Food & Drink"
    assert body["is_seeded"] is True


def test_patch_kind_rejected_422(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """kind is immutable — CategoryUpdate forbids it (extra="forbid")."""
    cat = Category(user_id=seeded_user.id, name="Coffee", kind="spend", is_seeded=False)
    session.add(cat)
    session.commit()
    session.refresh(cat)

    status_code, _ = _patch(client, cat.id, {"kind": "income"})
    assert status_code == 422


def test_patch_rename_collision_409(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    a = Category(user_id=seeded_user.id, name="A", is_seeded=False)
    b = Category(user_id=seeded_user.id, name="B", is_seeded=False)
    session.add_all([a, b])
    session.commit()
    session.refresh(b)

    status_code, body = _patch(client, b.id, {"name": "A"})
    assert status_code == 409
    assert body["detail"] == "category name already exists"


def test_patch_rename_to_same_name_noop(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    cat = Category(user_id=seeded_user.id, name="Coffee", is_seeded=False)
    session.add(cat)
    session.commit()
    session.refresh(cat)

    status_code, body = _patch(client, cat.id, {"name": "Coffee"})
    assert status_code == 200
    assert body["name"] == "Coffee"


def test_patch_empty_body_noop(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    cat = Category(user_id=seeded_user.id, name="Coffee", is_seeded=False)
    session.add(cat)
    session.commit()
    session.refresh(cat)

    status_code, body = _patch(client, cat.id, {})
    assert status_code == 200
    assert body["name"] == "Coffee"


def test_patch_unknown_id_404(
    client: TestClient,
    seeded_user: User,
) -> None:
    status_code, body = _patch(client, 9999, {"name": "X"})
    assert status_code == 404
    assert body["detail"] == "category not found"


def test_patch_foreign_user_404(
    client: TestClient,
    seeded_user: User,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    other = User(id=uuid4())
    session.add(other)
    session.flush()
    foreign = Category(user_id=other.id, name="Foreign", is_seeded=False)
    session.add(foreign)
    session.commit()
    session.refresh(foreign)

    status_code, body = _patch(client, foreign.id, {"name": "Hijacked"})
    assert status_code == 404
    assert body["detail"] == "category not found"

    with session_factory() as s:
        unchanged = s.get(Category, foreign.id)
        assert unchanged is not None
        assert unchanged.name == "Foreign"


def test_patch_archived_404(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    cat = Category(
        user_id=seeded_user.id,
        name="Coffee",
        is_seeded=False,
        archived_at=datetime.now(UTC),
    )
    session.add(cat)
    session.commit()
    session.refresh(cat)

    status_code, body = _patch(client, cat.id, {"name": "X"})
    assert status_code == 404
    assert body["detail"] == "category not found"


def test_patch_blank_name_422(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    cat = Category(user_id=seeded_user.id, name="Coffee", is_seeded=False)
    session.add(cat)
    session.commit()
    session.refresh(cat)

    status_code, _ = _patch(client, cat.id, {"name": "   "})
    assert status_code == 422


def test_patch_null_name_422(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """Regression: ``str | None`` skips ``min_length`` for ``None``.

    Without the explicit ``None`` reject in ``CategoryUpdate._strip``,
    Pydantic would accept ``{"name": null}`` and the route would setattr
    ``name=None`` on the NOT NULL column → 500.
    """
    cat = Category(user_id=seeded_user.id, name="Coffee", is_seeded=False)
    session.add(cat)
    session.commit()
    session.refresh(cat)

    resp = client.patch(f"/api/v1/categories/{cat.id}", json={"name": None})
    assert resp.status_code == 422
    assert "name cannot be cleared" in resp.text


def test_patch_set_color(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    cat = Category(user_id=seeded_user.id, name="Coffee", is_seeded=False)
    session.add(cat)
    session.commit()
    session.refresh(cat)

    status_code, body = _patch(client, cat.id, {"color": "#10b981"})
    assert status_code == 200
    assert body["color"] == "#10b981"


def test_patch_color_null_reverts_to_derived(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """Unlike name, an explicit null clears the picked color (revert to derived)."""
    cat = Category(user_id=seeded_user.id, name="Coffee", color="#f59e0b", is_seeded=False)
    session.add(cat)
    session.commit()
    session.refresh(cat)

    status_code, body = _patch(client, cat.id, {"color": None})
    assert status_code == 200
    assert body["color"] is None


def test_patch_invalid_color_422(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    cat = Category(user_id=seeded_user.id, name="Coffee", is_seeded=False)
    session.add(cat)
    session.commit()
    session.refresh(cat)

    status_code, _ = _patch(client, cat.id, {"color": "rebeccapurple"})
    assert status_code == 422


def test_patch_rejects_extra_fields(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    cat = Category(user_id=seeded_user.id, name="Coffee", is_seeded=False)
    session.add(cat)
    session.commit()
    session.refresh(cat)

    status_code, _ = _patch(client, cat.id, {"is_seeded": False})
    assert status_code == 422


# ---------------------------------------------------------------------------
# DELETE /categories/{id}
# ---------------------------------------------------------------------------


def test_delete_happy_sets_archived_at(
    client: TestClient,
    seeded_user: User,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    cat = Category(user_id=seeded_user.id, name="Coffee", is_seeded=False)
    session.add(cat)
    session.commit()
    session.refresh(cat)

    resp = client.delete(f"/api/v1/categories/{cat.id}")
    assert resp.status_code == 204
    assert resp.text == ""

    with session_factory() as s:
        row = s.get(Category, cat.id)
        assert row is not None
        assert row.archived_at is not None


def test_delete_unknown_404(client: TestClient, seeded_user: User) -> None:
    resp = client.delete("/api/v1/categories/9999")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "category not found"


def test_delete_foreign_user_404(
    client: TestClient,
    seeded_user: User,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    other = User(id=uuid4())
    session.add(other)
    session.flush()
    foreign = Category(user_id=other.id, name="Foreign", is_seeded=False)
    session.add(foreign)
    session.commit()
    session.refresh(foreign)

    resp = client.delete(f"/api/v1/categories/{foreign.id}")
    assert resp.status_code == 404

    with session_factory() as s:
        unchanged = s.get(Category, foreign.id)
        assert unchanged is not None
        assert unchanged.archived_at is None


def test_delete_already_archived_404(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    cat = Category(user_id=seeded_user.id, name="Coffee", is_seeded=False)
    session.add(cat)
    session.commit()
    session.refresh(cat)

    first = client.delete(f"/api/v1/categories/{cat.id}")
    second = client.delete(f"/api/v1/categories/{cat.id}")
    assert first.status_code == 204
    assert second.status_code == 404


def test_delete_keeps_merchant_tag_map_rows_but_hides_them(
    client: TestClient,
    seeded_user: User,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Archiving a category KEEPS its ``merchant_tag_map`` rows (incl. a
    user-authored ``pinned`` one) — no silent data loss — while the
    archived-category filter hides them from both the rules list and F3 prefill.
    """
    from app.services.merchant_alias import EMPTY_RESOLVER
    from app.services.tag_service import prefetch_tag_map

    target = Category(user_id=seeded_user.id, name="Coffee", is_seeded=False)
    other = Category(user_id=seeded_user.id, name="Books", is_seeded=False)
    session.add_all([target, other])
    session.commit()
    session.refresh(target)
    session.refresh(other)

    session.add_all(
        [
            # A user-authored pin — the one that used to be silently destroyed.
            MerchantTagMap(
                user_id=seeded_user.id,
                merchant_normalized="starbucks",
                category_id=target.id,
                pinned=True,
            ),
            MerchantTagMap(
                user_id=seeded_user.id,
                merchant_normalized="ccd",
                category_id=target.id,
            ),
            MerchantTagMap(
                user_id=seeded_user.id,
                merchant_normalized="crossword",
                category_id=other.id,
            ),
        ]
    )
    session.commit()

    resp = client.delete(f"/api/v1/categories/{target.id}")
    assert resp.status_code == 204

    with session_factory() as s:
        # All three rows survive — the archived category's pin is NOT destroyed.
        remaining = list(s.scalars(select(MerchantTagMap)))
        assert len(remaining) == 3
        # But F3 prefill no longer resurrects the archived bucket (the JOIN
        # filters archived categories); only the live category's merchant maps.
        prefill = prefetch_tag_map(s, user_id=seeded_user.id, resolver=EMPTY_RESOLVER)
        assert prefill == {"crossword": other.id}

    # And the rules list hides the archived category's rules.
    rules = client.get("/api/v1/rules").json()
    merchants = {r["merchant_normalized"] for r in rules}
    assert "starbucks" not in merchants
    assert "ccd" not in merchants
    assert "crossword" in merchants


def test_delete_keeps_transactions_link(
    client: TestClient,
    seeded_user: User,
    axis_account,  # noqa: ANN001 — fixture type already declared in conftest
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Soft-deleting a category leaves transactions pointing at it (archived row stays in table)."""
    cat = Category(user_id=seeded_user.id, name="Coffee", is_seeded=False)
    session.add(cat)
    session.commit()
    session.refresh(cat)

    txn = Transaction(
        user_id=seeded_user.id,
        account_id=axis_account.id,
        date=datetime(2026, 3, 5).date(),
        amount_paise=-8500,
        transaction_type="spend",
        merchant_raw="STARBUCKS",
        merchant_normalized="STARBUCKS",
        category_id=cat.id,
        fingerprint="ff" * 32,
        source="manual",
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)

    resp = client.delete(f"/api/v1/categories/{cat.id}")
    assert resp.status_code == 204

    with session_factory() as s:
        unchanged = s.get(Transaction, txn.id)
        assert unchanged is not None
        assert unchanged.category_id == cat.id


def test_patch_labels_on_txn_with_archived_category_still_works(
    client: TestClient,
    seeded_user: User,
    axis_account,  # noqa: ANN001
    session: Session,
) -> None:
    """A txn referencing an archived category must still accept label PATCHes.

    The category pre-flight only fires when ``category_id`` is in the update
    dict, so label-only edits work today. Locking it in guards against a future
    "tighten the pre-flight to also check the txn's existing category_id"
    change that would silently break unrelated edits on historically-tagged
    transactions.
    """
    cat = Category(user_id=seeded_user.id, name="Coffee", is_seeded=False)
    session.add(cat)
    session.commit()
    session.refresh(cat)

    txn = Transaction(
        user_id=seeded_user.id,
        account_id=axis_account.id,
        date=datetime(2026, 3, 5).date(),
        amount_paise=-8500,
        transaction_type="spend",
        merchant_raw="STARBUCKS",
        merchant_normalized="STARBUCKS",
        category_id=cat.id,
        fingerprint="aa" * 32,
        source="manual",
    )
    session.add(txn)
    session.commit()

    # Archive the category AFTER the txn was tagged with it.
    cat.archived_at = datetime.now(UTC)
    session.commit()

    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"labels": ["still-works"]})
    assert resp.status_code == 200
    assert [lab["name"] for lab in resp.json()["labels"]] == ["still-works"]


def test_delete_then_recreate_same_name(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    cat = Category(user_id=seeded_user.id, name="Coffee", is_seeded=False)
    session.add(cat)
    session.commit()
    session.refresh(cat)

    delete_resp = client.delete(f"/api/v1/categories/{cat.id}")
    assert delete_resp.status_code == 204

    status_code, body = _post(client, {"name": "Coffee"})
    assert status_code == 201
    assert body["name"] == "Coffee"


# ---------------------------------------------------------------------------
# Two-Level Hierarchy Tests
# ---------------------------------------------------------------------------


def test_create_subcategory_happy(
    client: TestClient,
    seeded_user: User,
) -> None:
    status_code, parent = _post(
        client,
        {"name": "Food & Dining", "kind": "spend", "color": "#d95926"},
    )
    assert status_code == 201
    assert parent["parent_id"] is None

    status_code, child = _post(
        client,
        {"name": "Online Food Delivery", "kind": "spend", "parent_id": parent["id"]},
    )
    assert status_code == 201
    assert child["name"] == "Online Food Delivery"
    assert child["parent_id"] == parent["id"]


def test_create_subcategory_parent_not_found_422(
    client: TestClient,
    seeded_user: User,
) -> None:
    status_code, body = _post(client, {"name": "Groceries", "parent_id": 99999})
    assert status_code == 422
    assert body["detail"] == "parent category not found"


def test_create_subcategory_foreign_user_parent_422(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    other = User(id=uuid4())
    session.add(other)
    session.flush()
    other_cat = Category(user_id=other.id, name="OtherParent", kind="spend")
    session.add(other_cat)
    session.commit()

    status_code, body = _post(client, {"name": "MyChild", "parent_id": other_cat.id})
    assert status_code == 422
    assert body["detail"] == "parent category not found"


def test_create_subcategory_kind_mismatch_422(
    client: TestClient,
    seeded_user: User,
) -> None:
    status_code, spend_parent = _post(client, {"name": "Food & Dining", "kind": "spend"})
    assert status_code == 201

    # Attempt to add income child under spend parent
    status_code, body = _post(
        client,
        {"name": "IncomeChild", "kind": "income", "parent_id": spend_parent["id"]},
    )
    assert status_code == 422
    assert body["detail"] == "category kind must match parent category kind"


def test_create_cannot_nest_3_levels_422(
    client: TestClient,
    seeded_user: User,
) -> None:
    status_code, parent = _post(client, {"name": "Level1", "kind": "spend"})
    assert status_code == 201
    status_code, child = _post(
        client,
        {"name": "Level2", "kind": "spend", "parent_id": parent["id"]},
    )
    assert status_code == 201

    # Attempt to create level 3
    status_code, body = _post(
        client,
        {"name": "Level3", "kind": "spend", "parent_id": child["id"]},
    )
    assert status_code == 422
    assert body["detail"] == "cannot nest category more than 2 levels deep"


def test_list_tree_view(
    client: TestClient,
    seeded_user: User,
) -> None:
    _, food = _post(client, {"name": "Food & Dining", "kind": "spend", "color": "#d95926"})
    _post(client, {"name": "Groceries", "kind": "spend", "parent_id": food["id"]})
    _post(client, {"name": "Online Food Delivery", "kind": "spend", "parent_id": food["id"]})
    _, bills = _post(client, {"name": "Bills & Utilities", "kind": "spend", "color": "#0e97c4"})

    resp = client.get("/api/v1/categories?tree=true")
    assert resp.status_code == 200
    tree = resp.json()
    assert len(tree) == 2  # 2 root parents

    food_node = next(n for n in tree if n["name"] == "Food & Dining")
    assert len(food_node["subcategories"]) == 2
    sub_names = {s["name"] for s in food_node["subcategories"]}
    assert sub_names == {"Groceries", "Online Food Delivery"}

    bills_node = next(n for n in tree if n["name"] == "Bills & Utilities")
    assert len(bills_node["subcategories"]) == 0


def test_patch_reparenting_and_validation(
    client: TestClient,
    seeded_user: User,
) -> None:
    _, p1 = _post(client, {"name": "Parent1", "kind": "spend"})
    _, p2 = _post(client, {"name": "Parent2", "kind": "spend"})
    _, child = _post(client, {"name": "Child", "kind": "spend", "parent_id": p1["id"]})

    # Reparent Child from Parent1 to Parent2
    resp = client.patch(f"/api/v1/categories/{child['id']}", json={"parent_id": p2["id"]})
    assert resp.status_code == 200
    assert resp.json()["parent_id"] == p2["id"]

    # Promote Child to root (parent_id = null)
    resp = client.patch(f"/api/v1/categories/{child['id']}", json={"parent_id": None})
    assert resp.status_code == 200
    assert resp.json()["parent_id"] is None

    # Self-parenting rejected
    resp = client.patch(f"/api/v1/categories/{p1['id']}", json={"parent_id": p1["id"]})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "category cannot be its own parent"


def test_delete_parent_cascades_archive(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    _, parent = _post(client, {"name": "ParentCat", "kind": "spend"})
    _, child1 = _post(client, {"name": "Child1", "kind": "spend", "parent_id": parent["id"]})
    _, child2 = _post(client, {"name": "Child2", "kind": "spend", "parent_id": parent["id"]})

    resp = client.delete(f"/api/v1/categories/{parent['id']}")
    assert resp.status_code == 204

    # Both parent and children are now archived
    list_resp = client.get("/api/v1/categories")
    assert list_resp.status_code == 200
    active_names = {c["name"] for c in list_resp.json()}
    assert "ParentCat" not in active_names
    assert "Child1" not in active_names
    assert "Child2" not in active_names

