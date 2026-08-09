"""End-to-end tests for ``/api/v1/rules`` (PRD §F3 / §F3a — learned auto-tag rules).

Covers the HTTP view of the two per-merchant memory tables (``merchant_tag_map``
and ``merchant_label_map``): grouping by merchant, the single-winner ``is_winner``
pick (incl. tie resolution), the ``prefills`` threshold, per-user isolation on
both GET and DELETE, and the deliberately-stricter ``Category.user_id`` join that
guards against a cross-tenant category-name leak.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import Category, Label, MerchantLabelMap, MerchantTagMap, User
from app.services.merchant_labels import LABEL_PREFILL_MIN
from app.services.tag_service import prefetch_tag_map


def _cat(session: Session, user_id, name: str) -> Category:
    cat = Category(user_id=user_id, name=name, is_seeded=False)
    session.add(cat)
    session.commit()
    session.refresh(cat)
    return cat


def _label(session: Session, user_id, name: str) -> Label:
    label = Label(user_id=user_id, name=name)
    session.add(label)
    session.commit()
    session.refresh(label)
    return label


def _merchant(body: list[dict], merchant: str) -> dict | None:
    return next((r for r in body if r["merchant_normalized"] == merchant), None)


# ---------------------------------------------------------------------------
# GET /rules
# ---------------------------------------------------------------------------


def test_list_empty(client: TestClient, seeded_user: User) -> None:
    resp = client.get("/api/v1/rules")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_response_shape(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    cat = _cat(session, seeded_user.id, "Food")
    label = _label(session, seeded_user.id, "online")
    session.add(
        MerchantTagMap(user_id=seeded_user.id, merchant_normalized="SWIGGY", category_id=cat.id)
    )
    session.add(
        MerchantLabelMap(user_id=seeded_user.id, merchant_normalized="SWIGGY", label_id=label.id)
    )
    session.commit()

    body = client.get("/api/v1/rules").json()
    assert len(body) == 1
    rule = body[0]
    assert set(rule.keys()) == {"merchant_normalized", "categories", "labels"}
    assert "user_id" not in rule
    assert set(rule["categories"][0].keys()) == {
        "id",
        "category_id",
        "category_name",
        "hit_count",
        "last_used",
        "is_winner",
        "pinned",
    }
    assert set(rule["labels"][0].keys()) == {
        "id",
        "label_id",
        "label_name",
        "hit_count",
        "last_used",
        "prefills",
        "prefill_threshold",
        "pinned",
    }
    assert rule["categories"][0]["category_name"] == "Food"
    assert rule["labels"][0]["label_name"] == "online"


def test_list_groups_and_sorts_by_merchant(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    food = _cat(session, seeded_user.id, "Food")
    label = _label(session, seeded_user.id, "online")
    # ZOMATO has only a category; AMAZON has only a label — both must appear,
    # and merchants sort ASC.
    session.add(
        MerchantTagMap(user_id=seeded_user.id, merchant_normalized="ZOMATO", category_id=food.id)
    )
    session.add(
        MerchantLabelMap(user_id=seeded_user.id, merchant_normalized="AMAZON", label_id=label.id)
    )
    session.commit()

    body = client.get("/api/v1/rules").json()
    assert [r["merchant_normalized"] for r in body] == ["AMAZON", "ZOMATO"]
    assert _merchant(body, "AMAZON")["categories"] == []
    assert len(_merchant(body, "AMAZON")["labels"]) == 1
    assert len(_merchant(body, "ZOMATO")["categories"]) == 1
    assert _merchant(body, "ZOMATO")["labels"] == []


def test_list_category_winner_is_highest_hit_count(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    food = _cat(session, seeded_user.id, "Food")
    gift = _cat(session, seeded_user.id, "Gift")
    session.add(
        MerchantTagMap(
            user_id=seeded_user.id, merchant_normalized="SWIGGY", category_id=food.id, hit_count=5
        )
    )
    session.add(
        MerchantTagMap(
            user_id=seeded_user.id, merchant_normalized="SWIGGY", category_id=gift.id, hit_count=2
        )
    )
    session.commit()

    cats = _merchant(client.get("/api/v1/rules").json(), "SWIGGY")["categories"]
    winners = [c for c in cats if c["is_winner"]]
    assert len(winners) == 1
    assert winners[0]["category_name"] == "Food"


def test_list_winner_tie_resolves_to_single_row(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """Equal hit_count + equal last_used → exactly one winner (id DESC tiebreak)."""
    food = _cat(session, seeded_user.id, "Food")
    gift = _cat(session, seeded_user.id, "Gift")
    ts = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    lo = MerchantTagMap(
        user_id=seeded_user.id,
        merchant_normalized="SWIGGY",
        category_id=food.id,
        hit_count=4,
        last_used=ts,
    )
    hi = MerchantTagMap(
        user_id=seeded_user.id,
        merchant_normalized="SWIGGY",
        category_id=gift.id,
        hit_count=4,
        last_used=ts,
    )
    session.add_all([lo, hi])
    session.commit()
    session.refresh(lo)
    session.refresh(hi)

    cats = _merchant(client.get("/api/v1/rules").json(), "SWIGGY")["categories"]
    winners = [c for c in cats if c["is_winner"]]
    assert len(winners) == 1
    # id DESC breaks the tie — the later-inserted row wins.
    assert winners[0]["id"] == max(lo.id, hi.id)


def test_list_label_prefills_threshold(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    hot = _label(session, seeded_user.id, "online")
    cold = _label(session, seeded_user.id, "rare")
    session.add(
        MerchantLabelMap(
            user_id=seeded_user.id,
            merchant_normalized="AMAZON",
            label_id=hot.id,
            hit_count=LABEL_PREFILL_MIN,
        )
    )
    session.add(
        MerchantLabelMap(
            user_id=seeded_user.id,
            merchant_normalized="AMAZON",
            label_id=cold.id,
            hit_count=LABEL_PREFILL_MIN - 1,
        )
    )
    session.commit()

    labels = _merchant(client.get("/api/v1/rules").json(), "AMAZON")["labels"]
    by_name = {lab["label_name"]: lab["prefills"] for lab in labels}
    assert by_name == {"online": True, "rare": False}
    # Each label carries the prefill bar so the client renders "n/N" from server
    # truth instead of a hardcoded 3 (#10).
    assert all(lab["prefill_threshold"] == LABEL_PREFILL_MIN for lab in labels)


def test_prefill_threshold_is_single_source_of_truth() -> None:
    """The category "confident" bar and the label prefill bar are one constant, so
    they can't drift apart (#10)."""
    from app.api.v1.imports import CONFIDENT_MIN
    from app.services.merchant_labels import LABEL_PREFILL_MIN as backend_bar

    assert backend_bar == CONFIDENT_MIN


def test_list_label_order_pinned_first(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """The label list uses the shared winner ordering (merchant_map_winner_order):
    a pinned label outranks a higher-hit_count unpinned one. Guards the label copy
    of the order-by against divergence from prefetch_tag_map (#8)."""
    pinned_low = _label(session, seeded_user.id, "authored")
    learned_high = _label(session, seeded_user.id, "frequent")
    session.add(
        MerchantLabelMap(
            user_id=seeded_user.id,
            merchant_normalized="AMAZON",
            label_id=pinned_low.id,
            hit_count=1,
            pinned=True,
        )
    )
    session.add(
        MerchantLabelMap(
            user_id=seeded_user.id,
            merchant_normalized="AMAZON",
            label_id=learned_high.id,
            hit_count=50,
        )
    )
    session.commit()

    labels = _merchant(client.get("/api/v1/rules").json(), "AMAZON")["labels"]
    # Pinned DESC wins the ordering even though its hit_count is far lower.
    assert [lab["label_name"] for lab in labels] == ["authored", "frequent"]


def test_list_omits_archived_category_rule(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    cat = _cat(session, seeded_user.id, "Food")
    cat.archived_at = datetime.now(UTC)
    session.add(
        MerchantTagMap(user_id=seeded_user.id, merchant_normalized="SWIGGY", category_id=cat.id)
    )
    session.commit()

    body = client.get("/api/v1/rules").json()
    # The merchant's only rule points at an archived category → merchant absent.
    assert _merchant(body, "SWIGGY") is None


def test_list_omits_foreign_user(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    other = User(id=uuid4())
    session.add(other)
    session.flush()
    other_cat = _cat(session, other.id, "Food")
    mine_cat = _cat(session, seeded_user.id, "Food")
    session.add(
        MerchantTagMap(user_id=other.id, merchant_normalized="THEIRS", category_id=other_cat.id)
    )
    session.add(
        MerchantTagMap(user_id=seeded_user.id, merchant_normalized="MINE", category_id=mine_cat.id)
    )
    session.commit()

    body = client.get("/api/v1/rules").json()
    assert [r["merchant_normalized"] for r in body] == ["MINE"]


def test_list_does_not_leak_cross_tenant_category_name(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """Regression guard for the stricter ``Category.user_id`` join.

    A corrupt same-user map row pointing at ANOTHER user's category must never
    surface that category's name (or the merchant) in this user's rules.
    """
    other = User(id=uuid4())
    session.add(other)
    session.flush()
    foreign_cat = _cat(session, other.id, "SecretBucket")
    # user_id is seeded_user (own row) but category_id belongs to `other`.
    session.add(
        MerchantTagMap(
            user_id=seeded_user.id,
            merchant_normalized="LEAKY",
            category_id=foreign_cat.id,
        )
    )
    session.commit()

    resp = client.get("/api/v1/rules")
    assert _merchant(resp.json(), "LEAKY") is None
    assert "SecretBucket" not in resp.text


def test_list_label_rule_omits_foreign_user(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """Label-map GET isolation — the mirror of ``test_list_omits_foreign_user``."""
    other = User(id=uuid4())
    session.add(other)
    session.flush()
    their_label = _label(session, other.id, "theirs")
    my_label = _label(session, seeded_user.id, "mine")
    session.add(
        MerchantLabelMap(user_id=other.id, merchant_normalized="THEIRS", label_id=their_label.id)
    )
    session.add(
        MerchantLabelMap(user_id=seeded_user.id, merchant_normalized="MINE", label_id=my_label.id)
    )
    session.commit()

    body = client.get("/api/v1/rules").json()
    assert [r["merchant_normalized"] for r in body] == ["MINE"]


def test_cross_tenant_label_rule_rejected_by_composite_fk(
    seeded_user: User,
    session: Session,
) -> None:
    """The category-style cross-tenant leak is *unrepresentable* for labels.

    Unlike ``merchant_tag_map`` (plain FK on ``category_id`` — a corrupt same-user
    row pointing at another user's category CAN exist, hence the
    ``Category.user_id`` join guard + its leak test), ``merchant_label_map`` has a
    composite ``(label_id, user_id) → labels(id, user_id)`` FK. That rejects a
    same-user map row pointing at another user's label at INSERT time, so the
    corrupt row can never reach the join. This test documents why no join-filter
    leak test is needed on the label side — the DB makes the leak impossible.
    """
    other = User(id=uuid4())
    session.add(other)
    session.flush()
    foreign_label = _label(session, other.id, "secrettag")
    # user_id is seeded_user (own row) but label_id belongs to `other` — the
    # composite FK has no matching (label_id, user_id) pair in `labels`.
    session.add(
        MerchantLabelMap(
            user_id=seeded_user.id,
            merchant_normalized="LEAKY",
            label_id=foreign_label.id,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_list_archived_category_coexists_with_active_label(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """A merchant whose category rule is archived but whose label rule is live
    still appears — with empty ``categories`` and the surviving label."""
    cat = _cat(session, seeded_user.id, "Food")
    cat.archived_at = datetime.now(UTC)
    label = _label(session, seeded_user.id, "online")
    session.add(
        MerchantTagMap(user_id=seeded_user.id, merchant_normalized="SWIGGY", category_id=cat.id)
    )
    session.add(
        MerchantLabelMap(user_id=seeded_user.id, merchant_normalized="SWIGGY", label_id=label.id)
    )
    session.commit()

    rule = _merchant(client.get("/api/v1/rules").json(), "SWIGGY")
    assert rule is not None
    assert rule["categories"] == []
    assert [lab["label_name"] for lab in rule["labels"]] == ["online"]


def test_list_winner_matches_prefetch_tag_map(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """Parity guard against ORDER BY drift: the ``is_winner`` category the API
    marks must be the exact row ``tag_service.prefetch_tag_map`` would prefill.

    Both hand-copy ``(hit_count DESC, last_used DESC, id DESC)``; this seeds the
    same rows through both paths so a future tiebreak change in one that isn't
    mirrored in the other fails here instead of silently diverging the badge."""
    food = _cat(session, seeded_user.id, "Food")
    gift = _cat(session, seeded_user.id, "Gift")
    ts = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    # Equal hit_count + equal last_used → the tiebreak (id DESC) decides; this is
    # exactly the case a naive max(hit_count) implementation would get wrong.
    session.add_all(
        [
            MerchantTagMap(
                user_id=seeded_user.id,
                merchant_normalized="SWIGGY",
                category_id=food.id,
                hit_count=4,
                last_used=ts,
            ),
            MerchantTagMap(
                user_id=seeded_user.id,
                merchant_normalized="SWIGGY",
                category_id=gift.id,
                hit_count=4,
                last_used=ts,
            ),
        ]
    )
    session.commit()

    prefill_winner = prefetch_tag_map(session, user_id=seeded_user.id)["SWIGGY"]
    cats = _merchant(client.get("/api/v1/rules").json(), "SWIGGY")["categories"]
    api_winner = next(c for c in cats if c["is_winner"])
    assert api_winner["category_id"] == prefill_winner


# ---------------------------------------------------------------------------
# DELETE /rules/categories/{id}
# ---------------------------------------------------------------------------


def test_delete_category_rule_happy(
    client: TestClient,
    seeded_user: User,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    cat = _cat(session, seeded_user.id, "Food")
    rule = MerchantTagMap(user_id=seeded_user.id, merchant_normalized="SWIGGY", category_id=cat.id)
    session.add(rule)
    session.commit()
    session.refresh(rule)

    resp = client.delete(f"/api/v1/rules/categories/{rule.id}")
    assert resp.status_code == 204
    assert resp.text == ""

    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(MerchantTagMap)) == 0
    # Re-delete is a 404 (row is gone).
    assert client.delete(f"/api/v1/rules/categories/{rule.id}").status_code == 404


def test_delete_category_rule_unknown_404(client: TestClient, seeded_user: User) -> None:
    resp = client.delete("/api/v1/rules/categories/9999")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "rule not found"


def test_delete_category_rule_foreign_user_404(
    client: TestClient,
    seeded_user: User,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    other = User(id=uuid4())
    session.add(other)
    session.flush()
    foreign_cat = _cat(session, other.id, "Food")
    foreign_rule = MerchantTagMap(
        user_id=other.id, merchant_normalized="THEIRS", category_id=foreign_cat.id
    )
    session.add(foreign_rule)
    session.commit()
    session.refresh(foreign_rule)

    resp = client.delete(f"/api/v1/rules/categories/{foreign_rule.id}")
    assert resp.status_code == 404
    with session_factory() as s:
        assert s.get(MerchantTagMap, foreign_rule.id) is not None


# ---------------------------------------------------------------------------
# DELETE /rules/labels/{id}
# ---------------------------------------------------------------------------


def test_delete_label_rule_happy(
    client: TestClient,
    seeded_user: User,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    label = _label(session, seeded_user.id, "online")
    rule = MerchantLabelMap(user_id=seeded_user.id, merchant_normalized="AMAZON", label_id=label.id)
    session.add(rule)
    session.commit()
    session.refresh(rule)

    resp = client.delete(f"/api/v1/rules/labels/{rule.id}")
    assert resp.status_code == 204
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(MerchantLabelMap)) == 0
    # The label catalog row itself is untouched — only the learned rule is gone.
    with session_factory() as s:
        assert s.get(Label, label.id) is not None


def test_delete_label_rule_foreign_user_404(
    client: TestClient,
    seeded_user: User,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    other = User(id=uuid4())
    session.add(other)
    session.flush()
    foreign_label = _label(session, other.id, "theirs")
    foreign_rule = MerchantLabelMap(
        user_id=other.id, merchant_normalized="THEIRS", label_id=foreign_label.id
    )
    session.add(foreign_rule)
    session.commit()
    session.refresh(foreign_rule)

    resp = client.delete(f"/api/v1/rules/labels/{foreign_rule.id}")
    assert resp.status_code == 404
    with session_factory() as s:
        assert s.get(MerchantLabelMap, foreign_rule.id) is not None


# ---------------------------------------------------------------------------
# POST /rules/categories  (pin / create / re-point)
# ---------------------------------------------------------------------------


def test_create_category_rule_pins_new_merchant(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    cat = _cat(session, seeded_user.id, "Food")

    resp = client.post(
        "/api/v1/rules/categories", json={"merchant": "Swiggy", "category_id": cat.id}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["merchant_normalized"] == "swiggy"  # server-normalized, echoed back
    assert body["pinned"] is True

    rule = _merchant(client.get("/api/v1/rules").json(), "swiggy")
    assert rule is not None
    cats = rule["categories"]
    assert len(cats) == 1
    assert cats[0]["is_winner"] is True
    assert cats[0]["pinned"] is True
    assert cats[0]["hit_count"] == 1  # authored floor


def test_create_category_rule_normalizes_merchant(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    cat = _cat(session, seeded_user.id, "Food")
    resp = client.post(
        "/api/v1/rules/categories", json={"merchant": "  BLUE   Tokai  ", "category_id": cat.id}
    )
    assert resp.status_code == 201
    assert resp.json()["merchant_normalized"] == "blue tokai"


def test_create_category_rule_pin_beats_higher_hit_count(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    """Pinning a category outranks a higher-hit_count learned row for the same
    merchant, and the learned row survives untouched (as a non-winner)."""
    food = _cat(session, seeded_user.id, "Food")
    gift = _cat(session, seeded_user.id, "Gift")
    session.add(
        MerchantTagMap(
            user_id=seeded_user.id, merchant_normalized="swiggy", category_id=food.id, hit_count=9
        )
    )
    session.commit()

    resp = client.post(
        "/api/v1/rules/categories", json={"merchant": "swiggy", "category_id": gift.id}
    )
    assert resp.status_code == 201

    cats = {
        c["category_name"]: c
        for c in _merchant(client.get("/api/v1/rules").json(), "swiggy")["categories"]
    }
    assert cats["Gift"]["is_winner"] is True
    assert cats["Gift"]["pinned"] is True
    assert cats["Food"]["is_winner"] is False
    assert cats["Food"]["pinned"] is False
    assert cats["Food"]["hit_count"] == 9  # learned row untouched


def test_create_category_rule_repoint_unpins_previous(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    food = _cat(session, seeded_user.id, "Food")
    gift = _cat(session, seeded_user.id, "Gift")
    client.post("/api/v1/rules/categories", json={"merchant": "amazon", "category_id": food.id})
    client.post("/api/v1/rules/categories", json={"merchant": "amazon", "category_id": gift.id})

    cats = {
        c["category_name"]: c
        for c in _merchant(client.get("/api/v1/rules").json(), "amazon")["categories"]
    }
    assert cats["Gift"]["pinned"] is True
    assert cats["Food"]["pinned"] is False
    # Exactly one winner.
    assert sum(1 for c in cats.values() if c["is_winner"]) == 1
    assert cats["Gift"]["is_winner"] is True


def test_create_category_rule_blank_merchant_422(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    cat = _cat(session, seeded_user.id, "Food")
    resp = client.post("/api/v1/rules/categories", json={"merchant": "   ", "category_id": cat.id})
    assert resp.status_code == 422


def test_create_category_rule_overlong_merchant_422(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    cat = _cat(session, seeded_user.id, "Food")
    resp = client.post(
        "/api/v1/rules/categories", json={"merchant": "x" * 513, "category_id": cat.id}
    )
    assert resp.status_code == 422


def test_create_category_rule_unknown_category_422(client: TestClient, seeded_user: User) -> None:
    resp = client.post("/api/v1/rules/categories", json={"merchant": "swiggy", "category_id": 9999})
    assert resp.status_code == 422


def test_create_category_rule_cross_user_category_422(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    other = User(id=uuid4())
    session.add(other)
    session.flush()
    foreign_cat = _cat(session, other.id, "Food")
    resp = client.post(
        "/api/v1/rules/categories", json={"merchant": "swiggy", "category_id": foreign_cat.id}
    )
    assert resp.status_code == 422


def test_create_category_rule_archived_category_422(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    cat = _cat(session, seeded_user.id, "Food")
    cat.archived_at = datetime.now(UTC)
    session.commit()
    resp = client.post(
        "/api/v1/rules/categories", json={"merchant": "swiggy", "category_id": cat.id}
    )
    assert resp.status_code == 422


def test_create_category_rule_income_category_422(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    """The merchant→category map is spend/refund-only, so pinning a merchant to
    an income category is rejected at the API (not just masked by the UI) — a
    durable pinned rule would otherwise re-pollute every future spend import."""
    income_cat = Category(user_id=seeded_user.id, name="Salary", kind="income", is_seeded=False)
    session.add(income_cat)
    session.commit()
    session.refresh(income_cat)
    resp = client.post(
        "/api/v1/rules/categories", json={"merchant": "acme payroll", "category_id": income_cat.id}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "category not found, archived, or not a spend category"
    # No rule was created.
    assert session.scalars(select(MerchantTagMap)).all() == []


# ---------------------------------------------------------------------------
# PATCH /rules/categories/{id}  (pin / un-pin toggle)
# ---------------------------------------------------------------------------


def test_patch_category_rule_pin_then_unpin(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    food = _cat(session, seeded_user.id, "Food")
    gift = _cat(session, seeded_user.id, "Gift")
    food_rule = MerchantTagMap(
        user_id=seeded_user.id, merchant_normalized="swiggy", category_id=food.id, hit_count=9
    )
    gift_rule = MerchantTagMap(
        user_id=seeded_user.id, merchant_normalized="swiggy", category_id=gift.id, hit_count=2
    )
    session.add_all([food_rule, gift_rule])
    session.commit()
    session.refresh(gift_rule)

    def winner_name() -> str:
        cats = _merchant(client.get("/api/v1/rules").json(), "swiggy")["categories"]
        return next(c["category_name"] for c in cats if c["is_winner"])

    assert winner_name() == "Food"  # higher hit_count wins while unpinned

    resp = client.patch(f"/api/v1/rules/categories/{gift_rule.id}", json={"pinned": True})
    assert resp.status_code == 200
    assert resp.json()["pinned"] is True
    assert winner_name() == "Gift"  # pin overrides hit_count

    resp = client.patch(f"/api/v1/rules/categories/{gift_rule.id}", json={"pinned": False})
    assert resp.status_code == 200
    assert winner_name() == "Food"  # un-pin reverts to learned ranking


def test_patch_category_rule_pin_unpins_live_sibling(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    """PATCH-pinning B while A is already pinned must un-pin A (single pinned
    category per merchant) — exercises set_tag_pinned's sibling-unpin against a
    *live* pinned sibling, which the pin-then-unpin test never does."""
    food = _cat(session, seeded_user.id, "Food")
    gift = _cat(session, seeded_user.id, "Gift")
    food_rule = MerchantTagMap(
        user_id=seeded_user.id,
        merchant_normalized="amazon",
        category_id=food.id,
        hit_count=3,
        pinned=True,
    )
    gift_rule = MerchantTagMap(
        user_id=seeded_user.id, merchant_normalized="amazon", category_id=gift.id, hit_count=1
    )
    session.add_all([food_rule, gift_rule])
    session.commit()
    session.refresh(gift_rule)

    resp = client.patch(f"/api/v1/rules/categories/{gift_rule.id}", json={"pinned": True})
    assert resp.status_code == 200

    cats = {
        c["category_name"]: c
        for c in _merchant(client.get("/api/v1/rules").json(), "amazon")["categories"]
    }
    assert cats["Gift"]["pinned"] is True
    assert cats["Food"]["pinned"] is False  # sibling un-pinned by the PATCH
    assert sum(1 for c in cats.values() if c["pinned"]) == 1


def test_patch_category_rule_unknown_404(client: TestClient, seeded_user: User) -> None:
    resp = client.patch("/api/v1/rules/categories/9999", json={"pinned": True})
    assert resp.status_code == 404


def test_patch_category_rule_cross_user_404(
    client: TestClient, seeded_user: User, session: Session, session_factory: sessionmaker[Session]
) -> None:
    other = User(id=uuid4())
    session.add(other)
    session.flush()
    foreign_cat = _cat(session, other.id, "Food")
    foreign_rule = MerchantTagMap(
        user_id=other.id, merchant_normalized="theirs", category_id=foreign_cat.id
    )
    session.add(foreign_rule)
    session.commit()
    session.refresh(foreign_rule)

    resp = client.patch(f"/api/v1/rules/categories/{foreign_rule.id}", json={"pinned": True})
    assert resp.status_code == 404
    with session_factory() as s:
        assert s.get(MerchantTagMap, foreign_rule.id).pinned is False  # untouched


# ---------------------------------------------------------------------------
# POST /rules/labels  +  PATCH /rules/labels/{id}
# ---------------------------------------------------------------------------


def test_create_label_rule_pins_below_threshold(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    """A freshly pinned label prefills immediately, even at hit_count=1 (< bar)."""
    label = _label(session, seeded_user.id, "online")
    resp = client.post("/api/v1/rules/labels", json={"merchant": "Amazon", "label_id": label.id})
    assert resp.status_code == 201
    assert resp.json()["merchant_normalized"] == "amazon"

    labels = _merchant(client.get("/api/v1/rules").json(), "amazon")["labels"]
    assert len(labels) == 1
    assert labels[0]["pinned"] is True
    assert labels[0]["prefills"] is True  # pinned → prefills despite hit_count=1


def test_create_label_rule_unknown_label_422(client: TestClient, seeded_user: User) -> None:
    resp = client.post("/api/v1/rules/labels", json={"merchant": "amazon", "label_id": 9999})
    assert resp.status_code == 422


def test_create_label_rule_cross_user_label_422(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    other = User(id=uuid4())
    session.add(other)
    session.flush()
    foreign_label = _label(session, other.id, "secret")
    resp = client.post(
        "/api/v1/rules/labels", json={"merchant": "amazon", "label_id": foreign_label.id}
    )
    assert resp.status_code == 422


def test_create_label_rule_blank_merchant_422(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    label = _label(session, seeded_user.id, "online")
    resp = client.post("/api/v1/rules/labels", json={"merchant": "  ", "label_id": label.id})
    assert resp.status_code == 422


def test_patch_label_rule_pin_then_unpin(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    label = _label(session, seeded_user.id, "online")
    rule = MerchantLabelMap(
        user_id=seeded_user.id, merchant_normalized="amazon", label_id=label.id, hit_count=1
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)

    def prefills() -> bool:
        labels = _merchant(client.get("/api/v1/rules").json(), "amazon")["labels"]
        return labels[0]["prefills"]

    assert prefills() is False  # below bar, unpinned

    assert client.patch(f"/api/v1/rules/labels/{rule.id}", json={"pinned": True}).status_code == 200
    assert prefills() is True

    assert (
        client.patch(f"/api/v1/rules/labels/{rule.id}", json={"pinned": False}).status_code == 200
    )
    assert prefills() is False


def test_patch_label_rule_unknown_404(client: TestClient, seeded_user: User) -> None:
    assert client.patch("/api/v1/rules/labels/9999", json={"pinned": True}).status_code == 404


# ---------------------------------------------------------------------------
# GET /rules/merchants  (create autocomplete source)
# ---------------------------------------------------------------------------


def test_list_rule_merchants_distinct_sorted_scoped(
    client: TestClient, seeded_user: User, session: Session
) -> None:
    cat = _cat(session, seeded_user.id, "Food")
    label = _label(session, seeded_user.id, "online")
    other = User(id=uuid4())
    session.add(other)
    session.flush()
    other_cat = _cat(session, other.id, "Food")

    session.add_all(
        [
            # Same merchant in both maps → must de-dupe to one entry.
            MerchantTagMap(
                user_id=seeded_user.id, merchant_normalized="zomato", category_id=cat.id
            ),
            MerchantLabelMap(
                user_id=seeded_user.id, merchant_normalized="zomato", label_id=label.id
            ),
            MerchantTagMap(
                user_id=seeded_user.id, merchant_normalized="amazon", category_id=cat.id
            ),
            # A blank merchant must be excluded.
            MerchantTagMap(user_id=seeded_user.id, merchant_normalized="", category_id=cat.id),
            # Another user's merchant must not appear.
            MerchantTagMap(
                user_id=other.id, merchant_normalized="theirs", category_id=other_cat.id
            ),
        ]
    )
    session.commit()

    resp = client.get("/api/v1/rules/merchants")
    assert resp.status_code == 200
    assert resp.json() == ["amazon", "zomato"]  # distinct, sorted, scoped, no blank
