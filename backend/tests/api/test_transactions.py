"""End-to-end tests for ``GET /api/v1/transactions`` (PRD §F1 read path).

Seeds rows directly via the ``session`` fixture (no need to round-trip
through ``POST /imports`` per test — that's covered by
``test_imports.py`` and ``test_slice.py``). Each test asserts one
contract of the read endpoint: filters, ordering, pagination, flat
shape.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    Account,
    Category,
    CategoryKindStr,
    Label,
    MerchantTagMap,
    Transaction,
    TransactionLabel,
    User,
)
from app.schemas.transactions import MAX_LABELS_PER_TXN
from app.services.fingerprint import transaction_fingerprint
from app.services.merchant import normalize_merchant

_WRONG_KIND_DETAIL = "category not found, archived, or wrong kind for this transaction"

# Sentinel for the "use now()" default — distinct from caller-supplied None
# (None = pending row in the review queue, intentionally).
_DEFAULT_CONFIRMED: Any = object()


def _make_txn(
    *,
    user_id: UUID,
    account_id: int,
    txn_date: date,
    amount_paise: int,
    fingerprint: str,
    transaction_type: str = "spend",
    merchant_raw: str = "TEST MERCHANT",
    category_id: int | None = None,
    confirmed_at: datetime | None = _DEFAULT_CONFIRMED,
) -> Transaction:
    # Route through the real normalize_merchant so any future test that
    # asserts fingerprint parity with import_service paths stays consistent.
    #
    # Default `confirmed_at = now()` keeps existing GET /transactions tests
    # green under the board's `confirmed_at IS NOT NULL` filter. Callers
    # building pending-queue rows pass `confirmed_at=None` explicitly.
    return Transaction(
        user_id=user_id,
        account_id=account_id,
        date=txn_date,
        amount_paise=amount_paise,
        transaction_type=transaction_type,
        merchant_raw=merchant_raw,
        merchant_normalized=normalize_merchant(merchant_raw),
        category_id=category_id,
        fingerprint=fingerprint,
        source="import",
        confirmed_at=(datetime.now(UTC) if confirmed_at is _DEFAULT_CONFIRMED else confirmed_at),
    )


def test_list_empty(
    client: TestClient,
    seeded_user: User,
) -> None:
    resp = client.get("/api/v1/transactions")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_newest_first(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 1, 1),
                amount_paise=-100,
                fingerprint="fp-jan",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 1),
                amount_paise=-200,
                fingerprint="fp-may",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 3, 1),
                amount_paise=-300,
                fingerprint="fp-mar",
            ),
        ]
    )
    session.commit()

    resp = client.get("/api/v1/transactions")
    assert resp.status_code == 200
    dates = [row["date"] for row in resp.json()]
    assert dates == ["2026-05-01", "2026-03-01", "2026-01-01"]


def test_list_same_date_tiebreak_by_id_desc(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    same_day = date(2026, 4, 15)
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=same_day,
                amount_paise=-100,
                fingerprint="fp-first",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=same_day,
                amount_paise=-200,
                fingerprint="fp-second",
            ),
        ]
    )
    session.commit()

    resp = client.get("/api/v1/transactions")
    assert resp.status_code == 200
    ids = [row["id"] for row in resp.json()]
    assert ids == sorted(ids, reverse=True)


def test_list_filter_by_account(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    card_a = Account(
        user_id=seeded_user.id,
        name="Card A",
        type="credit_card",
        issuer="axis",
        last4="1111",
    )
    card_b = Account(
        user_id=seeded_user.id,
        name="Card B",
        type="credit_card",
        issuer="axis",
        last4="2222",
    )
    session.add_all([card_a, card_b])
    session.commit()
    session.refresh(card_a)
    session.refresh(card_b)

    session.add_all(
        [
            _make_txn(
                user_id=seeded_user.id,
                account_id=card_a.id,
                txn_date=date(2026, 4, 1),
                amount_paise=-100,
                fingerprint="fp-a",
            ),
            _make_txn(
                user_id=seeded_user.id,
                account_id=card_b.id,
                txn_date=date(2026, 4, 1),
                amount_paise=-200,
                fingerprint="fp-b",
            ),
        ]
    )
    session.commit()

    resp = client.get(f"/api/v1/transactions?account_id={card_a.id}")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["account_id"] == card_a.id


def test_list_filter_by_category(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """Drilldown filter for the F8 spend-by-category surface."""
    food = next(c for c in seeded_categories if c.name == "Food")
    transport = next(c for c in seeded_categories if c.name == "Transport")
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 10),
                amount_paise=-15000,
                fingerprint="fp-food",
                category_id=food.id,
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 11),
                amount_paise=-9000,
                fingerprint="fp-transport",
                category_id=transport.id,
            ),
        ]
    )
    session.commit()

    resp = client.get(f"/api/v1/transactions?category_id={food.id}")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["category_id"] == food.id


def test_list_filter_by_label(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """?label_id=<id> returns only transactions carrying that label (EXISTS filter)."""
    tagged = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-15000,
        fingerprint="fp-tagged",
    )
    plain = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 11),
        amount_paise=-9000,
        fingerprint="fp-plain",
    )
    session.add_all([tagged, plain])
    session.commit()
    label = Label(user_id=axis_account.user_id, name="travel")
    session.add(label)
    session.flush()
    session.add(
        TransactionLabel(transaction_id=tagged.id, label_id=label.id, user_id=axis_account.user_id)
    )
    session.commit()

    resp = client.get(f"/api/v1/transactions?label_id={label.id}")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["id"] == tagged.id
    assert {lab["name"] for lab in rows[0]["labels"]} == {"travel"}


def test_list_filter_by_category_no_matches_returns_empty(
    client: TestClient,
    axis_account: Account,
    seeded_categories: list[Category],
    session: Session,
) -> None:
    """Unknown / cross-user / archived category_id returns []; no 404.

    Mirrors the ?account_id= filter: a filter with no matches yields an
    empty result rather than 404. Keeps the drilldown URL stable across
    "category just archived" or "user typo" races without making the
    frontend handle two distinct empty states.
    """
    session.add(
        _make_txn(
            user_id=axis_account.user_id,
            account_id=axis_account.id,
            txn_date=date(2026, 5, 10),
            amount_paise=-15000,
            fingerprint="fp-only",
            category_id=None,
        )
    )
    session.commit()

    # Defensively above any seeded id rather than a bare constant.
    unknown_id = max(c.id for c in seeded_categories) + 10_000
    resp = client.get(f"/api/v1/transactions?category_id={unknown_id}")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_filter_date_range_inclusive(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 1, 15),
                amount_paise=-100,
                fingerprint="fp-jan",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 3, 15),
                amount_paise=-200,
                fingerprint="fp-mar",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 15),
                amount_paise=-300,
                fingerprint="fp-may",
            ),
        ]
    )
    session.commit()

    resp = client.get("/api/v1/transactions?date_from=2026-03-15&date_to=2026-05-15")
    assert resp.status_code == 200
    dates = [row["date"] for row in resp.json()]
    assert set(dates) == {"2026-03-15", "2026-05-15"}


def test_list_invalid_date_range_422(
    client: TestClient,
    seeded_user: User,
) -> None:
    resp = client.get("/api/v1/transactions?date_from=2026-05-01&date_to=2026-01-01")
    assert resp.status_code == 422
    assert resp.json()["detail"] == "date_from must be <= date_to"


def test_list_pagination_offset_limit(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, day),
                amount_paise=-100 * day,
                fingerprint=f"fp-day-{day}",
            )
            for day in range(1, 6)
        ]
    )
    session.commit()

    resp = client.get("/api/v1/transactions?limit=2&offset=2")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2
    # Newest-first ordering — day 5 + day 4 are pages [0..1]; offset 2/limit 2 → day 3 + day 2.
    assert [row["date"] for row in rows] == ["2026-05-03", "2026-05-02"]


def test_list_limit_cap_rejected(
    client: TestClient,
    seeded_user: User,
) -> None:
    resp = client.get("/api/v1/transactions?limit=501")
    assert resp.status_code == 422


def test_list_flat_shape_omits_internal_fields(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    session.add(
        _make_txn(
            user_id=axis_account.user_id,
            account_id=axis_account.id,
            txn_date=date(2026, 4, 10),
            amount_paise=-99950,
            fingerprint="fp-shape",
        )
    )
    session.commit()

    resp = client.get("/api/v1/transactions")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert set(rows[0].keys()) == {
        "id",
        "account_id",
        "date",
        "amount_paise",
        "transaction_type",
        "merchant_raw",
        "category_id",
        "transfer_pair_id",
        "labels",
    }
    # Belt-and-braces: PRD §F4 says fingerprint stays internal; user_id
    # never leaks single-user-v1 → multi-user-v2 boundary either.
    assert "user_id" not in rows[0]
    assert "fingerprint" not in rows[0]
    assert "merchant_normalized" not in rows[0]
    # auto_category_id is an internal acceptance-metric field, never on the wire.
    assert "auto_category_id" not in rows[0]


def test_list_refund_serialises_correctly(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Refund (positive amount_paise, refund type) alongside same-date spend."""
    same_day = date(2026, 4, 20)
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=same_day,
                amount_paise=-45000,
                fingerprint="fp-spend",
                transaction_type="spend",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=same_day,
                amount_paise=99950,
                fingerprint="fp-refund",
                transaction_type="refund",
                merchant_raw="REFUND MERCHANT",
            ),
        ]
    )
    session.commit()

    resp = client.get("/api/v1/transactions")
    assert resp.status_code == 200
    by_type = {row["transaction_type"]: row for row in resp.json()}
    assert by_type["refund"]["amount_paise"] == 99950
    assert by_type["spend"]["amount_paise"] == -45000


def test_list_filter_by_transaction_type(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """`?transaction_type=spend&transaction_type=refund` excludes income.

    The /expenses board's server-side filter: income/transfer rows live on a
    separate surface, so the board requests spend+refund only. Also locks the
    §F4a sign invariant at the DB boundary (spend < 0, refund > 0) so the
    frontend's `amount_paise > 0 ⇒ credit` render has guaranteed-correct input.
    """
    day = date(2026, 5, 12)
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=day,
                amount_paise=-45000,
                fingerprint="fp-spend",
                transaction_type="spend",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=day,
                amount_paise=12000,
                fingerprint="fp-refund",
                transaction_type="refund",
                merchant_raw="REFUND MERCHANT",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=day,
                amount_paise=500000,
                fingerprint="fp-income",
                transaction_type="income",
                merchant_raw="PAYROLL",
            ),
        ]
    )
    session.commit()

    resp = client.get("/api/v1/transactions?transaction_type=spend&transaction_type=refund")
    assert resp.status_code == 200
    by_type = {row["transaction_type"]: row for row in resp.json()}
    assert set(by_type) == {"spend", "refund"}  # income excluded
    # §F4a sign invariant at the wire boundary.
    assert by_type["spend"]["amount_paise"] < 0
    assert by_type["refund"]["amount_paise"] > 0


def test_list_filter_transaction_type_omitted_returns_all(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Omitting the param → None → no filter (show all types).

    The safe "show everything" path is *omitting* the param, not sending it
    empty: `?transaction_type=` parses to `[""]` and 422s on the Literal (see
    the bogus/empty test below). The frontend must omit, not blank, to get all.
    """
    session.add_all(
        [
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 12),
                amount_paise=-45000,
                fingerprint="fp-spend",
                transaction_type="spend",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 12),
                amount_paise=500000,
                fingerprint="fp-income",
                transaction_type="income",
                merchant_raw="PAYROLL",
            ),
        ]
    )
    session.commit()

    resp = client.get("/api/v1/transactions")
    assert resp.status_code == 200
    assert len(resp.json()) == 2  # both types returned — no filter applied


def test_list_filter_transaction_type_transfer_only(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """``?transaction_type=transfer`` returns both legs of a pair, nothing else.

    This is the exact param the board's Transfers filter chip emits. Transfer
    rows are otherwise unreachable from the UI: they are excluded from the
    Spending and Income views by construction, so without this filter a
    mis-detected F4a auto-link has no surface to be inspected or deleted from.
    """
    bank_leg = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 12),
        amount_paise=-250000,
        fingerprint="fp-transfer-bank",
        transaction_type="transfer",
        merchant_raw="CC PAYMENT",
    )
    card_leg = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 12),
        amount_paise=250000,
        fingerprint="fp-transfer-card",
        transaction_type="transfer",
        merchant_raw="PAYMENT RECEIVED",
    )
    session.add_all(
        [
            bank_leg,
            card_leg,
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 12),
                amount_paise=-45000,
                fingerprint="fp-spend",
                transaction_type="spend",
            ),
            _make_txn(
                user_id=axis_account.user_id,
                account_id=axis_account.id,
                txn_date=date(2026, 5, 12),
                amount_paise=500000,
                fingerprint="fp-income",
                transaction_type="income",
                merchant_raw="PAYROLL",
            ),
        ]
    )
    # Pair the two legs symmetrically, as auto_link_cc_bill does — ids only
    # exist after the flush, so the link is a second step.
    session.flush()
    bank_leg.transfer_pair_id = card_leg.id
    card_leg.transfer_pair_id = bank_leg.id
    session.commit()

    resp = client.get("/api/v1/transactions?transaction_type=transfer")
    assert resp.status_code == 200
    body = resp.json()
    # Both legs present, spend/income excluded. `transfer_pair_id` is not on the
    # wire yet (it lands with the board's unlink control), so the legs are
    # identified by merchant.
    assert {row["transaction_type"] for row in body} == {"transfer"}
    assert {row["merchant_raw"] for row in body} == {
        "CC PAYMENT",
        "PAYMENT RECEIVED",
    }


@pytest.mark.parametrize("value", ["bogus", ""])
def test_list_filter_transaction_type_invalid_422(
    value: str,
    client: TestClient,
    seeded_user: User,
) -> None:
    """Unknown value AND an empty value both 422 via the Literal type.

    `?transaction_type=` → `[""]`; `""` is not a member of TransactionTypeStr,
    so it's rejected with no manual validation.
    """
    resp = client.get(f"/api/v1/transactions?transaction_type={value}")
    assert resp.status_code == 422


def _seed_one(
    session: Session,
    *,
    axis_account: Account,
    fingerprint: str = "fp-patch",
    labels: tuple[str, ...] = (),
) -> Transaction:
    txn = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 4, 10),
        amount_paise=-12345,
        fingerprint=fingerprint,
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    for name in labels:
        lbl = Label(user_id=axis_account.user_id, name=name)
        session.add(lbl)
        session.flush()
        session.add(
            TransactionLabel(transaction_id=txn.id, label_id=lbl.id, user_id=axis_account.user_id)
        )
    if labels:
        session.commit()
    return txn


def test_patch_transaction_sets_labels(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    txn = _seed_one(session, axis_account=axis_account)
    resp = client.patch(
        f"/api/v1/transactions/{txn.id}", json={"labels": ["#Online", "restaurant"]}
    )
    assert resp.status_code == 200
    # Names are normalized (lowercased, '#' stripped) and returned as objects.
    assert [lab["name"] for lab in resp.json()["labels"]] == ["online", "restaurant"]

    follow = client.get("/api/v1/transactions").json()
    assert {lab["name"] for lab in follow[0]["labels"]} == {"online", "restaurant"}


def test_patch_transaction_replace_set(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """PATCH labels is a REPLACE-set: {a,b} → {b,c} removes a, keeps b, adds c."""
    txn = _seed_one(session, axis_account=axis_account, labels=("a", "b"))
    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"labels": ["b", "c"]})
    assert resp.status_code == 200
    assert {lab["name"] for lab in resp.json()["labels"]} == {"b", "c"}


def test_patch_transaction_clears_labels_with_empty_list(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    txn = _seed_one(session, axis_account=axis_account, labels=("keep",))
    pre = client.get("/api/v1/transactions").json()
    assert {lab["name"] for lab in pre[0]["labels"]} == {"keep"}

    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"labels": []})
    assert resp.status_code == 200
    assert resp.json()["labels"] == []


def test_patch_transaction_clears_labels_with_null(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """PATCH labels=null clears the set — the `| None` is load-bearing (null =
    clear, omitted = leave-alone via exclude_unset)."""
    txn = _seed_one(session, axis_account=axis_account, labels=("keep",))
    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"labels": None})
    assert resp.status_code == 200
    assert resp.json()["labels"] == []


def test_patch_transaction_label_too_long_422(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """A name over 64 chars 422s at the boundary (matches POST /labels) rather
    than silently truncating in normalize_label_name."""
    txn = _seed_one(session, axis_account=axis_account)
    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"labels": ["x" * 65]})
    assert resp.status_code == 422


def test_patch_transaction_too_many_labels_422(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """A list over MAX_LABELS_PER_TXN 422s before resolve_label_names builds an
    oversized IN clause."""
    txn = _seed_one(session, axis_account=axis_account)
    labels = [f"t{i}" for i in range(MAX_LABELS_PER_TXN + 1)]
    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"labels": labels})
    assert resp.status_code == 422


def test_post_transaction_label_too_long_422(
    client: TestClient,
    axis_account: Account,
) -> None:
    resp = client.post(
        "/api/v1/transactions", json=_post_body(account_id=axis_account.id, labels=["x" * 65])
    )
    assert resp.status_code == 422


def test_post_transaction_too_many_labels_422(
    client: TestClient,
    axis_account: Account,
) -> None:
    labels = [f"t{i}" for i in range(MAX_LABELS_PER_TXN + 1)]
    resp = client.post(
        "/api/v1/transactions", json=_post_body(account_id=axis_account.id, labels=labels)
    )
    assert resp.status_code == 422


def test_patch_transaction_omitted_labels_no_change(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """exclude_unset: a PATCH that omits `labels` must NOT clear existing labels."""
    cat = _seed_category(session, axis_account.user_id)
    txn = _seed_one(session, axis_account=axis_account, labels=("keep",))
    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"category_id": cat.id})
    assert resp.status_code == 200
    assert {lab["name"] for lab in resp.json()["labels"]} == {"keep"}


def test_patch_transaction_unknown_id_404(
    client: TestClient,
    seeded_user: User,
) -> None:
    resp = client.patch("/api/v1/transactions/9999", json={"labels": ["x"]})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "transaction not found"


def test_patch_transaction_unknown_field_422(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """TransactionUpdate has extra='forbid' so typos surface immediately."""
    txn = _seed_one(session, axis_account=axis_account)
    resp = client.patch(
        f"/api/v1/transactions/{txn.id}",
        json={"label": ["typo"]},  # singular — would silently no-op without forbid
    )
    assert resp.status_code == 422


def test_patch_transaction_foreign_user_returns_404(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """A txn belonging to another user is unreachable via PATCH (404, not leak)."""
    other_user = User(id=uuid4())
    session.add(other_user)
    session.flush()
    foreign_account = Account(
        user_id=other_user.id,
        name="Foreign CC",
        type="credit_card",
        issuer="axis",
        last4="9999",
    )
    session.add(foreign_account)
    session.flush()
    foreign_txn = _make_txn(
        user_id=other_user.id,
        account_id=foreign_account.id,
        txn_date=date(2026, 4, 1),
        amount_paise=-100,
        fingerprint="fp-foreign",
    )
    session.add(foreign_txn)
    session.commit()
    session.refresh(foreign_txn)

    resp = client.patch(f"/api/v1/transactions/{foreign_txn.id}", json={"labels": ["hack"]})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "transaction not found"

    # The rejected request created no label (get-or-create never ran).
    assert session.scalars(select(Label)).all() == []


# ---------------------------------------------------------------------------
# PATCH /transactions/{id} — category_id edits
# ---------------------------------------------------------------------------


def _seed_category(
    session: Session,
    user_id: UUID,
    name: str = "Food",
    kind: CategoryKindStr = "spend",
) -> Category:
    cat = Category(user_id=user_id, name=name, kind=kind, is_seeded=False)
    session.add(cat)
    session.commit()
    session.refresh(cat)
    return cat


def test_patch_transaction_sets_category_id(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    txn = _seed_one(session, axis_account=axis_account)
    cat = _seed_category(session, axis_account.user_id)

    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"category_id": cat.id})
    assert resp.status_code == 200
    assert resp.json()["category_id"] == cat.id

    follow = client.get("/api/v1/transactions").json()
    assert follow[0]["category_id"] == cat.id


def test_patch_transaction_clears_category_id_with_null(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    cat = _seed_category(session, axis_account.user_id)
    txn = _seed_one(session, axis_account=axis_account)
    txn.category_id = cat.id
    session.commit()

    pre = client.get("/api/v1/transactions").json()
    assert pre[0]["category_id"] == cat.id

    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"category_id": None})
    assert resp.status_code == 200
    assert resp.json()["category_id"] is None


def test_patch_transaction_omitted_category_id_no_change(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """exclude_unset honours omission — patching only `labels` leaves category alone."""
    cat = _seed_category(session, axis_account.user_id)
    txn = _seed_one(session, axis_account=axis_account)
    txn.category_id = cat.id
    session.commit()

    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"labels": ["x"]})
    assert resp.status_code == 200
    assert resp.json()["category_id"] == cat.id


def test_patch_transaction_unknown_category_422(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    txn = _seed_one(session, axis_account=axis_account)
    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"category_id": 99999})
    assert resp.status_code == 422
    assert resp.json()["detail"] == _WRONG_KIND_DETAIL


def test_patch_transaction_foreign_user_category_422(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Cross-user category id is rejected — FK alone would NOT catch this."""
    other = User(id=uuid4())
    session.add(other)
    session.flush()
    foreign_cat = Category(user_id=other.id, name="ForeignFood", is_seeded=False)
    session.add(foreign_cat)
    session.commit()
    session.refresh(foreign_cat)

    txn = _seed_one(session, axis_account=axis_account)
    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"category_id": foreign_cat.id})
    assert resp.status_code == 422
    assert resp.json()["detail"] == _WRONG_KIND_DETAIL


def test_patch_transaction_archived_category_422(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    cat = _seed_category(session, axis_account.user_id)
    cat.archived_at = datetime.now(UTC)
    session.commit()

    txn = _seed_one(session, axis_account=axis_account)
    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"category_id": cat.id})
    assert resp.status_code == 422
    assert resp.json()["detail"] == _WRONG_KIND_DETAIL


def test_patch_transaction_both_fields_atomic(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    cat = _seed_category(session, axis_account.user_id)
    txn = _seed_one(session, axis_account=axis_account)

    resp = client.patch(
        f"/api/v1/transactions/{txn.id}",
        json={"labels": ["lunch"], "category_id": cat.id},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [lab["name"] for lab in body["labels"]] == ["lunch"]
    assert body["category_id"] == cat.id


def test_patch_transaction_invalid_category_id_short_circuits_before_writes(
    client: TestClient,
    axis_account: Account,
    session: Session,
    session_factory,  # noqa: ANN001
) -> None:
    """Pre-flight rejects the bad category_id before any label/column write.

    Labels stay at their prior value because nothing was written — the
    category pre-flight 422s before the setattr loop and the label replace.
    """
    txn = _seed_one(session, axis_account=axis_account, labels=("initial",))

    resp = client.patch(
        f"/api/v1/transactions/{txn.id}",
        json={"labels": ["new"], "category_id": 99999},
    )
    assert resp.status_code == 422

    with session_factory() as s:
        fresh = s.get(Transaction, txn.id)
        assert fresh is not None
        assert {lab.name for lab in fresh.labels} == {"initial"}
        assert fresh.category_id is None


# ---------------------------------------------------------------------------
# PATCH /transactions/{id} — F3 tag-map learning
# ---------------------------------------------------------------------------


def test_patch_category_records_new_tag(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """The map row is created AND the txn's category_id persists on the same PATCH.

    Asserting txn.category_id is what would catch the SAVEPOINT regression —
    a naive ``session.rollback()`` inside ``record_tag``'s race-recovery
    path would silently revert the route's setattr before commit.
    """
    txn = _seed_one(session, axis_account=axis_account)
    cat = _seed_category(session, axis_account.user_id)

    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"category_id": cat.id})
    assert resp.status_code == 200
    assert resp.json()["category_id"] == cat.id

    session.expire_all()
    persisted = session.get(Transaction, txn.id)
    assert persisted is not None
    assert persisted.category_id == cat.id

    rows = session.scalars(select(MerchantTagMap)).all()
    assert len(rows) == 1
    assert rows[0].merchant_normalized == txn.merchant_normalized
    assert rows[0].category_id == cat.id
    assert rows[0].hit_count == 1


def test_patch_distinct_category_increments_existing_tag(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """A second PATCH that changes category back to a previously-used one bumps hit_count.

    Cycle Food → Subscriptions → Food. Final PATCH lands on a category
    that already has a map row → that row's hit_count bumps from 1 to 2.
    Mirrors a real "I changed my mind, going back to Food" user flow.
    """
    txn = _seed_one(session, axis_account=axis_account)
    food = _seed_category(session, axis_account.user_id, name="Food")
    subs = _seed_category(session, axis_account.user_id, name="Subscriptions")

    # Tag as Food → inserts row A (hit_count=1).
    assert (
        client.patch(f"/api/v1/transactions/{txn.id}", json={"category_id": food.id}).status_code
        == 200
    )
    food_row = session.scalar(select(MerchantTagMap).where(MerchantTagMap.category_id == food.id))
    assert food_row is not None
    first_last_used = food_row.last_used

    # Move to Subscriptions, then back to Food → bumps row A (hit_count=2).
    assert (
        client.patch(f"/api/v1/transactions/{txn.id}", json={"category_id": subs.id}).status_code
        == 200
    )
    assert (
        client.patch(f"/api/v1/transactions/{txn.id}", json={"category_id": food.id}).status_code
        == 200
    )

    session.expire_all()
    food_after = session.scalar(select(MerchantTagMap).where(MerchantTagMap.category_id == food.id))
    assert food_after is not None
    assert food_after.hit_count == 2
    assert food_after.last_used.replace(tzinfo=None) >= first_last_used.replace(tzinfo=None)


def test_patch_same_category_does_not_bump_hit_count(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Re-PATCHing the same category is not a new decision → hit_count stays.

    Prevents frontend retry storms / user double-clicks from inflating
    hit_count beyond the actual number of distinct user decisions.
    """
    txn = _seed_one(session, axis_account=axis_account)
    cat = _seed_category(session, axis_account.user_id)

    # First PATCH establishes the tag.
    assert (
        client.patch(f"/api/v1/transactions/{txn.id}", json={"category_id": cat.id}).status_code
        == 200
    )
    # Second PATCH to the same category → no-change short-circuit.
    assert (
        client.patch(f"/api/v1/transactions/{txn.id}", json={"category_id": cat.id}).status_code
        == 200
    )

    session.expire_all()
    rows = session.scalars(select(MerchantTagMap)).all()
    assert len(rows) == 1
    assert rows[0].hit_count == 1


def test_patch_retag_leaves_old_tag_alone(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    txn = _seed_one(session, axis_account=axis_account)
    food = _seed_category(session, axis_account.user_id, name="Food")
    subs = _seed_category(session, axis_account.user_id, name="Subscriptions")

    # Tag as Food first.
    assert (
        client.patch(f"/api/v1/transactions/{txn.id}", json={"category_id": food.id}).status_code
        == 200
    )
    # Retag to Subscriptions.
    assert (
        client.patch(f"/api/v1/transactions/{txn.id}", json={"category_id": subs.id}).status_code
        == 200
    )

    session.expire_all()
    rows = {r.category_id: r for r in session.scalars(select(MerchantTagMap)).all()}
    assert set(rows) == {food.id, subs.id}
    assert rows[food.id].hit_count == 1  # untouched on retag
    assert rows[subs.id].hit_count == 1


def test_patch_clear_category_does_not_record(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    cat = _seed_category(session, axis_account.user_id)
    txn = _seed_one(session, axis_account=axis_account)
    txn.category_id = cat.id
    session.commit()

    # Pre-existing tag-map row should NOT be inserted by the route — only the
    # clear is happening here. Confirm the map stays empty before and after.
    assert session.scalars(select(MerchantTagMap)).all() == []

    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"category_id": None})
    assert resp.status_code == 200
    assert resp.json()["category_id"] is None

    assert session.scalars(select(MerchantTagMap)).all() == []


def test_patch_labels_only_does_not_touch_tag_map(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Labels are orthogonal to the F3 merchant→category map — a labels-only
    PATCH must not write a merchant_tag_map row."""
    txn = _seed_one(session, axis_account=axis_account)

    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"labels": ["lunch"]})
    assert resp.status_code == 200

    assert session.scalars(select(MerchantTagMap)).all() == []


# ---------------------------------------------------------------------------
# POST /transactions — F2 manual entry
# ---------------------------------------------------------------------------


def _post_body(
    *,
    account_id: int,
    amount_paise: int = -8500,
    transaction_type: str = "spend",
    merchant_raw: str = "Chai-wala",
    txn_date: str = "2026-05-24",
    category_id: int | None = None,
    labels: list[str] | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "date": txn_date,
        "account_id": account_id,
        "amount_paise": amount_paise,
        "transaction_type": transaction_type,
        "merchant_raw": merchant_raw,
    }
    if category_id is not None:
        body["category_id"] = category_id
    if labels is not None:
        body["labels"] = labels
    return body


def test_post_spend_happy_path(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    resp = client.post("/api/v1/transactions", json=_post_body(account_id=axis_account.id))
    assert resp.status_code == 201
    body = resp.json()
    assert set(body.keys()) == {
        "id",
        "account_id",
        "date",
        "amount_paise",
        "transaction_type",
        "merchant_raw",
        "category_id",
        "transfer_pair_id",
        "labels",
    }
    assert body["amount_paise"] == -8500
    assert body["transaction_type"] == "spend"
    assert body["merchant_raw"] == "Chai-wala"

    persisted = session.scalar(select(Transaction).where(Transaction.id == body["id"]))
    assert persisted is not None
    assert persisted.source == "manual"
    assert persisted.import_batch_id is None
    # merchant_normalized matches the pure normalizer; fingerprint matches the
    # exact PRD §F4 formula. Recomputing both locks F1↔F2 parity.
    assert persisted.merchant_normalized == normalize_merchant("Chai-wala")
    assert persisted.fingerprint == transaction_fingerprint(
        txn_date=date(2026, 5, 24),
        amount_paise=-8500,
        normalized_merchant=normalize_merchant("Chai-wala"),
        account_id=axis_account.id,
    )


def test_post_with_category_sets_tag_map(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    cat = _seed_category(session, axis_account.user_id)
    resp = client.post(
        "/api/v1/transactions",
        json=_post_body(account_id=axis_account.id, category_id=cat.id),
    )
    assert resp.status_code == 201
    assert resp.json()["category_id"] == cat.id

    session.expire_all()
    persisted = session.scalar(select(Transaction))
    assert persisted is not None
    assert persisted.category_id == cat.id

    rows = session.scalars(select(MerchantTagMap)).all()
    assert len(rows) == 1
    assert rows[0].merchant_normalized == "chai-wala"
    assert rows[0].category_id == cat.id
    assert rows[0].hit_count == 1


def test_post_income_positive_amount(
    client: TestClient,
    axis_account: Account,
    session: Session,  # noqa: ARG001 — fixture activates the DB schema
) -> None:
    resp = client.post(
        "/api/v1/transactions",
        json=_post_body(
            account_id=axis_account.id,
            amount_paise=100000,
            transaction_type="income",
            merchant_raw="ACME Payroll",
        ),
    )
    assert resp.status_code == 201
    assert resp.json()["transaction_type"] == "income"
    assert resp.json()["amount_paise"] == 100000


def test_post_refund_positive_amount(
    client: TestClient,
    axis_account: Account,
    session: Session,  # noqa: ARG001
) -> None:
    resp = client.post(
        "/api/v1/transactions",
        json=_post_body(
            account_id=axis_account.id,
            amount_paise=5000,
            transaction_type="refund",
            merchant_raw="Amazon",
        ),
    )
    assert resp.status_code == 201
    assert resp.json()["transaction_type"] == "refund"


@pytest.mark.parametrize("amount", [-50000, 50000])
def test_post_transfer_either_sign(
    amount: int,
    client: TestClient,
    axis_account: Account,
    session: Session,  # noqa: ARG001
) -> None:
    resp = client.post(
        "/api/v1/transactions",
        json=_post_body(
            account_id=axis_account.id,
            amount_paise=amount,
            transaction_type="transfer",
            merchant_raw=f"Transfer {amount}",
        ),
    )
    assert resp.status_code == 201


def test_post_spend_positive_amount_422(
    client: TestClient,
    axis_account: Account,
) -> None:
    resp = client.post(
        "/api/v1/transactions",
        json=_post_body(account_id=axis_account.id, amount_paise=5000, transaction_type="spend"),
    )
    assert resp.status_code == 422
    assert "spend requires negative" in resp.text


def test_post_income_negative_amount_422(
    client: TestClient,
    axis_account: Account,
) -> None:
    resp = client.post(
        "/api/v1/transactions",
        json=_post_body(account_id=axis_account.id, amount_paise=-5000, transaction_type="income"),
    )
    assert resp.status_code == 422
    assert "income requires positive" in resp.text


@pytest.mark.parametrize("txn_type", ["spend", "income", "refund", "transfer"])
def test_post_zero_amount_422(
    txn_type: str,
    client: TestClient,
    axis_account: Account,
) -> None:
    resp = client.post(
        "/api/v1/transactions",
        json=_post_body(account_id=axis_account.id, amount_paise=0, transaction_type=txn_type),
    )
    assert resp.status_code == 422
    assert "non-zero" in resp.text


def test_post_no_merchant_happy_path(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """A manual row may have no merchant: omitted, ``""`` and whitespace all
    persist ``merchant_raw IS NULL`` with ``merchant_normalized == ""``.

    Inverts the old empty/whitespace-only 422 contract (§A merchant-optional).
    NULL is the honest "no merchant" representation; the derived match key
    ``merchant_normalized`` stays a string so the PRD §F4 fingerprint holds.
    """
    # 1) merchant_raw omitted entirely.
    body = _post_body(account_id=axis_account.id)
    del body["merchant_raw"]
    resp = client.post("/api/v1/transactions", json=body)
    assert resp.status_code == 201
    assert resp.json()["merchant_raw"] is None

    persisted = session.scalar(select(Transaction).where(Transaction.id == resp.json()["id"]))
    assert persisted is not None
    assert persisted.merchant_raw is None
    assert persisted.merchant_normalized == ""

    # 2) explicit "" — same persisted shape, different amount so the
    # fingerprint doesn't collide with the omitted-merchant row above.
    resp_blank = client.post(
        "/api/v1/transactions",
        json=_post_body(account_id=axis_account.id, merchant_raw="", amount_paise=-1234),
    )
    assert resp_blank.status_code == 201
    assert resp_blank.json()["merchant_raw"] is None

    persisted_blank = session.scalar(
        select(Transaction).where(Transaction.id == resp_blank.json()["id"])
    )
    assert persisted_blank is not None
    assert persisted_blank.merchant_raw is None
    assert persisted_blank.merchant_normalized == ""


def test_post_long_merchant_at_cap_accepted(
    client: TestClient,
    axis_account: Account,
) -> None:
    """256 chars accepted; 257 rejected. Schema cap → DB column cap symmetry."""
    at_cap = "a" * 256
    over_cap = "a" * 257

    resp_ok = client.post(
        "/api/v1/transactions",
        json=_post_body(account_id=axis_account.id, merchant_raw=at_cap),
    )
    assert resp_ok.status_code == 201

    resp_bad = client.post(
        "/api/v1/transactions",
        json=_post_body(
            account_id=axis_account.id,
            merchant_raw=over_cap,
            amount_paise=-1234,  # different so fingerprint doesn't collide
        ),
    )
    assert resp_bad.status_code == 422


def test_post_unknown_account_422(
    client: TestClient,
    seeded_user: User,  # noqa: ARG001 — seeds the v1 user
) -> None:
    resp = client.post("/api/v1/transactions", json=_post_body(account_id=99999))
    assert resp.status_code == 422
    assert resp.json()["detail"] == "account not found or archived"


def test_post_foreign_user_account_422(
    client: TestClient,
    session: Session,
) -> None:
    other = User(id=uuid4())
    session.add(other)
    session.flush()
    foreign = Account(user_id=other.id, name="Foreign", type="bank", issuer=None, last4=None)
    session.add(foreign)
    session.commit()
    session.refresh(foreign)

    resp = client.post("/api/v1/transactions", json=_post_body(account_id=foreign.id))
    assert resp.status_code == 422
    assert resp.json()["detail"] == "account not found or archived"


def test_post_archived_account_422(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    axis_account.archived_at = datetime.now(UTC)
    session.commit()

    resp = client.post("/api/v1/transactions", json=_post_body(account_id=axis_account.id))
    assert resp.status_code == 422
    assert resp.json()["detail"] == "account not found or archived"


def test_post_investment_account_422(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    investment = Account(
        user_id=seeded_user.id,
        name="Zerodha",
        type="investment",
        issuer=None,
        last4=None,
    )
    session.add(investment)
    session.commit()
    session.refresh(investment)

    resp = client.post("/api/v1/transactions", json=_post_body(account_id=investment.id))
    assert resp.status_code == 422
    assert resp.json()["detail"] == "transactions cannot be posted to investment accounts"


def test_post_unknown_category_422(
    client: TestClient,
    axis_account: Account,
) -> None:
    resp = client.post(
        "/api/v1/transactions",
        json=_post_body(account_id=axis_account.id, category_id=99999),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == _WRONG_KIND_DETAIL


def test_post_archived_category_422(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    cat = _seed_category(session, axis_account.user_id)
    cat.archived_at = datetime.now(UTC)
    session.commit()

    resp = client.post(
        "/api/v1/transactions",
        json=_post_body(account_id=axis_account.id, category_id=cat.id),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == _WRONG_KIND_DETAIL


def test_post_foreign_user_category_422(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    other = User(id=uuid4())
    session.add(other)
    session.flush()
    foreign_cat = Category(user_id=other.id, name="ForeignFood", is_seeded=False)
    session.add(foreign_cat)
    session.commit()
    session.refresh(foreign_cat)

    resp = client.post(
        "/api/v1/transactions",
        json=_post_body(account_id=axis_account.id, category_id=foreign_cat.id),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == _WRONG_KIND_DETAIL


def test_post_spend_with_income_category_422(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """A spend row pointing at an income category is rejected at the API — kind
    matching is now DB-query enforced, not UI-only."""
    income_cat = _seed_category(session, axis_account.user_id, name="Salary", kind="income")
    resp = client.post(
        "/api/v1/transactions",
        json=_post_body(
            account_id=axis_account.id, transaction_type="spend", category_id=income_cat.id
        ),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == _WRONG_KIND_DETAIL


def test_patch_spend_with_income_category_422(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """PATCHing an income category onto a spend row is rejected (mirror of POST)."""
    txn = _seed_one(session, axis_account=axis_account)  # spend by default
    income_cat = _seed_category(session, axis_account.user_id, name="Salary", kind="income")
    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"category_id": income_cat.id})
    assert resp.status_code == 422
    assert resp.json()["detail"] == _WRONG_KIND_DETAIL


def test_post_duplicate_fingerprint_409(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Second POST with identical date+amount+merchant+account returns 409.

    Also asserts the parent transaction rolled back atomically: if the second
    POST had `category_id` set, the tag-map state from that failed attempt
    must NOT be visible (only the first POST's tag-map row should remain).
    """
    cat = _seed_category(session, axis_account.user_id)
    body = _post_body(account_id=axis_account.id, category_id=cat.id)

    first = client.post("/api/v1/transactions", json=body)
    assert first.status_code == 201

    second = client.post("/api/v1/transactions", json=body)
    assert second.status_code == 409
    assert second.json()["detail"] == "transaction already exists"

    # Atomicity guard: the failed second POST must not have bumped hit_count.
    session.expire_all()
    rows = session.scalars(select(MerchantTagMap)).all()
    assert len(rows) == 1
    assert rows[0].hit_count == 1


def test_post_extra_field_422(
    client: TestClient,
    axis_account: Account,
) -> None:
    body = _post_body(account_id=axis_account.id)
    body["foo"] = "bar"
    resp = client.post("/api/v1/transactions", json=body)
    assert resp.status_code == 422


def test_post_without_category_no_tag_map_row(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    resp = client.post("/api/v1/transactions", json=_post_body(account_id=axis_account.id))
    assert resp.status_code == 201

    assert session.scalars(select(MerchantTagMap)).all() == []


def test_post_transfer_with_category_does_not_learn(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Manual entry is type-gated like the import auto-tag (PRD §F3).

    Only spend/refund feed the merchant→category map. A transfer (or income)
    row with a category must NOT learn a tag: income/transfer are hand-classified
    in a separate taxonomy and would only ever resurface as a cross-taxonomy
    spend suggestion. This reverses the earlier type-agnostic manual-entry rule;
    the gate lives at the POST/PATCH call sites via ``AUTO_TAGGABLE_TYPES``.
    """
    cat = _seed_category(session, axis_account.user_id, name="Rent")
    resp = client.post(
        "/api/v1/transactions",
        json=_post_body(
            account_id=axis_account.id,
            amount_paise=-50000,
            transaction_type="transfer",
            merchant_raw="Transfer to landlord",
            category_id=cat.id,
        ),
    )
    assert resp.status_code == 201

    session.expire_all()
    assert session.scalars(select(MerchantTagMap)).all() == []


def test_post_income_with_category_does_not_learn(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Income with a category must not write a tag-map row (PRD §F3 gate).

    Learning "merchant → <income category>" would mis-suggest that category on
    a future spend from the same merchant, so the POST path skips it.
    """
    cat = _seed_category(session, axis_account.user_id, name="Salary", kind="income")
    resp = client.post(
        "/api/v1/transactions",
        json=_post_body(
            account_id=axis_account.id,
            amount_paise=50000,  # income requires positive amount_paise
            transaction_type="income",
            merchant_raw="ACME Payroll",
            category_id=cat.id,
        ),
    )
    assert resp.status_code == 201
    assert resp.json()["category_id"] == cat.id  # gate skips learning, not the category

    session.expire_all()
    assert session.scalars(select(MerchantTagMap)).all() == []


def test_patch_income_category_does_not_learn(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """PATCHing a category onto an income row must not write a tag-map row.

    Mirrors the POST gate on the update path — the merchant→category map is
    spend/refund only (``AUTO_TAGGABLE_TYPES``). The category still persists on
    the txn; only the tag-map learning is skipped.
    """
    income = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 4, 12),
        amount_paise=50000,
        fingerprint="fp-income-patch",
        transaction_type="income",
        merchant_raw="ACME Payroll",
    )
    session.add(income)
    session.commit()
    session.refresh(income)
    cat = _seed_category(session, axis_account.user_id, name="Salary", kind="income")

    resp = client.patch(f"/api/v1/transactions/{income.id}", json={"category_id": cat.id})
    assert resp.status_code == 200
    assert resp.json()["category_id"] == cat.id

    session.expire_all()
    persisted = session.get(Transaction, income.id)
    assert persisted is not None
    assert persisted.category_id == cat.id
    assert session.scalars(select(MerchantTagMap)).all() == []


def test_post_labels_round_trip(
    client: TestClient,
    axis_account: Account,
) -> None:
    body = _post_body(account_id=axis_account.id, labels=["#Cash", "chai"])
    resp = client.post("/api/v1/transactions", json=body)
    assert resp.status_code == 201
    txn_id = resp.json()["id"]
    # Auto-created + normalized on the created row.
    assert {lab["name"] for lab in resp.json()["labels"]} == {"cash", "chai"}

    listing = client.get("/api/v1/transactions")
    row = next(r for r in listing.json() if r["id"] == txn_id)
    assert {lab["name"] for lab in row["labels"]} == {"cash", "chai"}


def test_post_duplicate_fingerprint_with_labels_409(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """A duplicate POST that also carries labels still maps to 409, not 500 —
    the label get-or-create + flush sit inside the fingerprint→409 guard."""
    body = _post_body(account_id=axis_account.id, labels=["online"])
    assert client.post("/api/v1/transactions", json=body).status_code == 201
    second = client.post("/api/v1/transactions", json=body)
    assert second.status_code == 409
    assert second.json()["detail"] == "transaction already exists"


def test_post_tag_race_recovery_keeps_txn(
    client: TestClient,
    axis_account: Account,
    session: Session,
    session_factory,  # noqa: ANN001 — sessionmaker injected from conftest
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fault-injection: simulate ``record_tag``'s INSERT-then-IntegrityError
    fallback path and assert the POST's transaction insert survives the
    SAVEPOINT rollback.

    TestClient with StaticPool serializes through one connection — this is
    NOT a real two-connection concurrent race. We inject the race window by
    (a) pre-inserting the conflicting tag-map row via a parallel session, and
    (b) monkeypatching ``Session.scalar`` so ``record_tag``'s existence probe
    misses once, forcing the INSERT branch to fire and trip the unique
    constraint. The real production scenario this exercises lives in
    multi-worker Postgres v2; in v0.1 SQLite the race window is essentially
    zero, but the recovery code path matters for the eventual Postgres swap.
    """
    from sqlalchemy.orm import Session as SaSession

    cat = _seed_category(session, axis_account.user_id)

    # Pre-insert tag-map row via parallel session so the route's record_tag
    # will hit the unique constraint when it tries to INSERT.
    other = session_factory()
    try:
        other.add(
            MerchantTagMap(
                user_id=axis_account.user_id,
                merchant_normalized="chai-wala",
                category_id=cat.id,
                hit_count=1,
            )
        )
        other.commit()
    finally:
        other.close()

    # Patch Session.scalar to return None on its FIRST call within record_tag's
    # existence probe — only patching the class method so the account/category
    # pre-flight SELECTs (which run first inside the same request session)
    # behave normally. Track the call count manually.
    real_scalar = SaSession.scalar
    state = {"miss_remaining": 1, "probe_signature": "merchant_tag_map"}

    def stub_scalar(self: SaSession, *args: object, **kwargs: object) -> object:
        # Heuristic: only stub the SELECT against merchant_tag_map (the probe).
        # Account + category pre-flight SELECTs touch other tables.
        if state["miss_remaining"] > 0 and args and "merchant_tag_map" in str(args[0]).lower():
            state["miss_remaining"] -= 1
            return None
        return real_scalar(self, *args, **kwargs)

    monkeypatch.setattr(SaSession, "scalar", stub_scalar)

    resp = client.post(
        "/api/v1/transactions",
        json=_post_body(account_id=axis_account.id, category_id=cat.id),
    )
    assert resp.status_code == 201
    txn_id = resp.json()["id"]

    # Fresh session read for both assertions.
    monkeypatch.undo()
    session.expire_all()
    persisted = session.get(Transaction, txn_id)
    assert persisted is not None
    assert persisted.category_id == cat.id

    rows = session.scalars(select(MerchantTagMap)).all()
    assert len(rows) == 1
    assert rows[0].hit_count == 2


def test_post_whitespace_collision_409(
    client: TestClient,
    axis_account: Account,
) -> None:
    """Two merchants differing only in surrounding whitespace dedupe.

    ``normalize_merchant`` strips + collapses whitespace, so ``"Swiggy "``
    and ``"Swiggy"`` produce the same normalized → same fingerprint → 409
    on the second POST. Locks the contract that user-visible merchant
    typos in whitespace don't create duplicate rows.
    """
    first = client.post(
        "/api/v1/transactions",
        json=_post_body(account_id=axis_account.id, merchant_raw="Swiggy"),
    )
    assert first.status_code == 201

    second = client.post(
        "/api/v1/transactions",
        json=_post_body(account_id=axis_account.id, merchant_raw="  Swiggy  "),
    )
    assert second.status_code == 409


def test_post_then_patch_tag_map_increments_via_post(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """PATCH learning + POST learning compose: same (merchant, category) bumps once each.

    Round-trips the cross-route invariant: POST inserts the tag-map row,
    a later PATCH of a *different* transaction to the same (merchant, category)
    pair increments hit_count rather than creating a second row. The unique
    constraint ``uq_merchant_tag_map_user_merchant_category`` is what keeps
    the map flat across F2 + F3 PATCH callers.
    """
    cat = _seed_category(session, axis_account.user_id)

    # POST a manual txn → inserts merchant_tag_map row (hit_count=1).
    resp = client.post(
        "/api/v1/transactions",
        json=_post_body(account_id=axis_account.id, category_id=cat.id),
    )
    assert resp.status_code == 201

    # Seed a SECOND transaction with the same merchant_normalized but a
    # different fingerprint (different amount) so we have a row to PATCH.
    other = _seed_one(
        session,
        axis_account=axis_account,
        fingerprint="fp-other-amount",
    )
    other.merchant_raw = "Chai-wala"
    other.merchant_normalized = normalize_merchant("Chai-wala")
    session.commit()

    # PATCH it to the same category → record_tag finds the existing map row
    # and bumps hit_count rather than inserting a duplicate.
    patch = client.patch(f"/api/v1/transactions/{other.id}", json={"category_id": cat.id})
    assert patch.status_code == 200

    session.expire_all()
    rows = session.scalars(select(MerchantTagMap)).all()
    assert len(rows) == 1
    assert rows[0].hit_count == 2


def test_post_non_fingerprint_integrity_error_propagates(
    client: TestClient,
    axis_account: Account,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`is_fp_dup` heuristic only catches fingerprint conflicts → other
    IntegrityErrors must NOT be translated to 409.

    Fault-injection: monkeypatch ``Session.flush`` to raise an IntegrityError
    whose ``orig`` text doesn't match the fingerprint heuristic. The route now
    flushes the pending Transaction (to assign its id for the label links) inside
    the same try that maps fingerprint dups to 409, so a non-fingerprint
    IntegrityError surfaces there; the `raise` branch must re-raise rather than
    mask a real schema bug as a friendly 409. TestClient re-raises server
    exceptions by default — we assert the exception propagates rather than
    checking a status code, which is the stronger guarantee (no silent 200/409).
    """
    from sqlalchemy.orm import Session as SaSession

    class _FakeOrig(Exception):
        def __str__(self) -> str:
            return "FOREIGN KEY constraint failed"

    real_flush = SaSession.flush

    def boom(self: SaSession, *args: object, **kwargs: object) -> None:
        # Fail the route's flush of the pending Transaction. The flushed row
        # leaves ``self.new``, so keying on it fires exactly once — at the route's
        # id-assigning flush inside the 409 guard (labels/category are absent in
        # this body, so no earlier flush carries a pending Transaction).
        if any(isinstance(obj, Transaction) for obj in self.new):
            raise IntegrityError("INSERT INTO transactions", {}, _FakeOrig())
        real_flush(self, *args, **kwargs)

    monkeypatch.setattr(SaSession, "flush", boom)

    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        client.post("/api/v1/transactions", json=_post_body(account_id=axis_account.id))


# ---------------------------------------------------------------------------
# DELETE /transactions/{id}
# ---------------------------------------------------------------------------


def test_delete_transaction_removes_row(
    client: TestClient,
    axis_account: Account,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    txn = _seed_one(session, axis_account=axis_account)
    txn_id = txn.id

    resp = client.delete(f"/api/v1/transactions/{txn_id}")
    assert resp.status_code == 204
    assert resp.content == b""

    # Fresh session — the test fixture's `session` still has the deleted
    # instance in its identity map, so .get() raises ObjectDeletedError.
    with session_factory() as s:
        assert s.scalar(select(Transaction).where(Transaction.id == txn_id)) is None

    follow = client.get("/api/v1/transactions").json()
    assert all(row["id"] != txn_id for row in follow)


def test_delete_transaction_cascades_labels(
    client: TestClient,
    axis_account: Account,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Deleting a transaction cascades its transaction_labels links (ON DELETE
    CASCADE), but the labels themselves survive for other transactions."""
    txn = _seed_one(session, axis_account=axis_account, labels=("travel", "goa"))

    resp = client.delete(f"/api/v1/transactions/{txn.id}")
    assert resp.status_code == 204

    with session_factory() as s:
        assert s.scalars(select(TransactionLabel)).all() == []  # links gone
        assert {lab.name for lab in s.scalars(select(Label))} == {"travel", "goa"}  # labels kept


def test_delete_transaction_unknown_returns_404(
    client: TestClient,
    seeded_user: User,
) -> None:
    resp = client.delete("/api/v1/transactions/99999")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "transaction not found"


def test_delete_transaction_foreign_user_returns_404(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """A txn belonging to another user is unreachable via DELETE (404, no mutation)."""
    other_user = User(id=uuid4())
    session.add(other_user)
    session.flush()
    foreign_account = Account(
        user_id=other_user.id,
        name="Foreign CC",
        type="credit_card",
        issuer="axis",
        last4="9999",
    )
    session.add(foreign_account)
    session.flush()
    foreign_txn = _make_txn(
        user_id=other_user.id,
        account_id=foreign_account.id,
        txn_date=date(2026, 4, 1),
        amount_paise=-100,
        fingerprint="fp-foreign-delete",
    )
    session.add(foreign_txn)
    session.commit()
    foreign_id = foreign_txn.id

    resp = client.delete(f"/api/v1/transactions/{foreign_id}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "transaction not found"

    # Confirm the foreign row is still present.
    session.expire_all()
    assert session.get(Transaction, foreign_id) is not None


def test_delete_transaction_paired_unlinks_partner(
    client: TestClient,
    axis_account: Account,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """DELETE on a paired row nulls the partner's transfer_pair_id atomically.

    Pre-flight added in the F4a-1 PR so deleting one half of an F4a auto-link
    doesn't trip the composite FK from migration 0005. The partner's
    transaction_type stays 'transfer' — PRD doesn't pin a restoration value
    and no provenance column exists. Cosmetic anomaly, not data corruption;
    user can manually relabel via a future widened PATCH.
    """
    # Seed two cross-linked rows directly via ORM (no F4a service call —
    # this test is about the DELETE route, not the link path). Both rows
    # belong to the same user, satisfying the composite FK invariant from
    # PR-B.
    cc_row = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=500000,
        fingerprint="fp-pair-cc",
        transaction_type="transfer",
    )
    session.add(cc_row)
    session.flush()

    # Bank account in the same user's space — the partner can live on any
    # account; we only need the composite FK target (id, user_id) to match.
    bank_row = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-500000,
        fingerprint="fp-pair-bank",
        transaction_type="transfer",
    )
    session.add(bank_row)
    session.flush()

    cc_row.transfer_pair_id = bank_row.id
    bank_row.transfer_pair_id = cc_row.id
    session.commit()
    cc_id, bank_id = cc_row.id, bank_row.id

    # DELETE the CC row.
    resp = client.delete(f"/api/v1/transactions/{cc_id}")
    assert resp.status_code == 204, resp.text

    # Fresh session — the test session still has cc_row in its identity map.
    with session_factory() as s:
        assert s.scalar(select(Transaction).where(Transaction.id == cc_id)) is None
        partner = s.scalar(select(Transaction).where(Transaction.id == bank_id))
        assert partner is not None
        assert partner.transfer_pair_id is None
        # Type stays 'transfer' — we don't restore.
        assert partner.transaction_type == "transfer"


# ---------------------------------------------------------------------------
# POST /transactions/transfer (PRD §F2 manual transfer)
# ---------------------------------------------------------------------------


def _transfer_body(
    *,
    source_account_id: int,
    dest_account_id: int,
    amount_paise: int = 500000,
    txn_date: str = "2026-05-24",
) -> dict[str, object]:
    return {
        "date": txn_date,
        "source_account_id": source_account_id,
        "dest_account_id": dest_account_id,
        "amount_paise": amount_paise,
    }


def test_transfer_happy_path(
    client: TestClient,
    bank_account: Account,
    axis_account: Account,
    session_factory: sessionmaker[Session],
) -> None:
    resp = client.post(
        "/api/v1/transactions/transfer",
        json=_transfer_body(source_account_id=bank_account.id, dest_account_id=axis_account.id),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body.keys()) == {"source", "dest"}
    leg_keys = {
        "id",
        "account_id",
        "date",
        "amount_paise",
        "transaction_type",
        "merchant_raw",
        "category_id",
        "transfer_pair_id",
        "labels",
    }
    assert set(body["source"].keys()) == leg_keys
    assert set(body["dest"].keys()) == leg_keys

    # Server-derived signs: source out (negative), dest in (positive).
    assert body["source"]["amount_paise"] == -500000
    assert body["dest"]["amount_paise"] == 500000
    assert body["source"]["account_id"] == bank_account.id
    assert body["dest"]["account_id"] == axis_account.id
    assert body["source"]["transaction_type"] == "transfer"
    assert body["dest"]["transaction_type"] == "transfer"
    # Auto-generated labels from the account names.
    assert body["source"]["merchant_raw"] == "Transfer to Axis CC"
    assert body["dest"]["merchant_raw"] == "Transfer from HDFC Bank"

    # Fresh session — the route's writes aren't in the test session identity map.
    with session_factory() as s:
        src = s.scalar(select(Transaction).where(Transaction.id == body["source"]["id"]))
        dst = s.scalar(select(Transaction).where(Transaction.id == body["dest"]["id"]))
        assert src is not None and dst is not None
        # Symmetry — ADR-0002 §3, both directions of the pair.
        assert src.transfer_pair_id == dst.id
        assert dst.transfer_pair_id == src.id
        for leg in (src, dst):
            assert leg.source == "manual"
            assert leg.import_batch_id is None
            assert leg.confirmed_at is not None  # born on the board
            assert leg.category_id is None
        # One event, one timestamp — both legs share the route's single now().
        assert src.confirmed_at == dst.confirmed_at


def test_transfer_fingerprint_parity(
    client: TestClient,
    bank_account: Account,
    axis_account: Account,
    session_factory: sessionmaker[Session],
) -> None:
    """Lock the PRD §F4 fingerprint formula for both legs (date + signed amount
    + auto-merchant + account_id)."""
    resp = client.post(
        "/api/v1/transactions/transfer",
        json=_transfer_body(source_account_id=bank_account.id, dest_account_id=axis_account.id),
    )
    assert resp.status_code == 201
    body = resp.json()
    with session_factory() as s:
        src = s.scalar(select(Transaction).where(Transaction.id == body["source"]["id"]))
        dst = s.scalar(select(Transaction).where(Transaction.id == body["dest"]["id"]))
        assert src is not None and dst is not None
        assert src.fingerprint == transaction_fingerprint(
            txn_date=date(2026, 5, 24),
            amount_paise=-500000,
            normalized_merchant=normalize_merchant("Transfer to Axis CC"),
            account_id=bank_account.id,
        )
        assert dst.fingerprint == transaction_fingerprint(
            txn_date=date(2026, 5, 24),
            amount_paise=500000,
            normalized_merchant=normalize_merchant("Transfer from HDFC Bank"),
            account_id=axis_account.id,
        )


@pytest.mark.parametrize("amount", [0, -100])
def test_transfer_non_positive_amount_422(
    client: TestClient,
    bank_account: Account,
    axis_account: Account,
    amount: int,
) -> None:
    resp = client.post(
        "/api/v1/transactions/transfer",
        json=_transfer_body(
            source_account_id=bank_account.id,
            dest_account_id=axis_account.id,
            amount_paise=amount,
        ),
    )
    assert resp.status_code == 422


def test_transfer_same_account_422(
    client: TestClient,
    axis_account: Account,
) -> None:
    resp = client.post(
        "/api/v1/transactions/transfer",
        json=_transfer_body(source_account_id=axis_account.id, dest_account_id=axis_account.id),
    )
    assert resp.status_code == 422


def test_transfer_extra_field_422(
    client: TestClient,
    bank_account: Account,
    axis_account: Account,
) -> None:
    body = _transfer_body(source_account_id=bank_account.id, dest_account_id=axis_account.id)
    body["foo"] = "bar"
    resp = client.post("/api/v1/transactions/transfer", json=body)
    assert resp.status_code == 422


def test_transfer_unknown_source_422(
    client: TestClient,
    axis_account: Account,
) -> None:
    resp = client.post(
        "/api/v1/transactions/transfer",
        json=_transfer_body(source_account_id=99999, dest_account_id=axis_account.id),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "source account not found or archived"


def test_transfer_archived_source_422(
    client: TestClient,
    bank_account: Account,
    axis_account: Account,
    session: Session,
) -> None:
    bank_account.archived_at = datetime.now(UTC)
    session.commit()

    resp = client.post(
        "/api/v1/transactions/transfer",
        json=_transfer_body(source_account_id=bank_account.id, dest_account_id=axis_account.id),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "source account not found or archived"


def test_transfer_foreign_user_source_422(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    other = User(id=uuid4())
    session.add(other)
    session.flush()
    foreign = Account(user_id=other.id, name="Foreign", type="bank", issuer=None, last4=None)
    session.add(foreign)
    session.commit()
    session.refresh(foreign)

    resp = client.post(
        "/api/v1/transactions/transfer",
        json=_transfer_body(source_account_id=foreign.id, dest_account_id=axis_account.id),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "source account not found or archived"


def test_transfer_unknown_dest_422(
    client: TestClient,
    bank_account: Account,
) -> None:
    resp = client.post(
        "/api/v1/transactions/transfer",
        json=_transfer_body(source_account_id=bank_account.id, dest_account_id=99999),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "destination account not found or archived"


def test_transfer_source_investment_422(
    client: TestClient,
    axis_account: Account,
    seeded_user: User,
    session: Session,
) -> None:
    investment = Account(
        user_id=seeded_user.id, name="Zerodha", type="investment", issuer=None, last4=None
    )
    session.add(investment)
    session.commit()
    session.refresh(investment)

    resp = client.post(
        "/api/v1/transactions/transfer",
        json=_transfer_body(source_account_id=investment.id, dest_account_id=axis_account.id),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "transfers cannot involve investment accounts"


def test_transfer_dest_investment_422(
    client: TestClient,
    bank_account: Account,
    seeded_user: User,
    session: Session,
) -> None:
    investment = Account(
        user_id=seeded_user.id, name="Zerodha", type="investment", issuer=None, last4=None
    )
    session.add(investment)
    session.commit()
    session.refresh(investment)

    resp = client.post(
        "/api/v1/transactions/transfer",
        json=_transfer_body(source_account_id=bank_account.id, dest_account_id=investment.id),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "transfers cannot involve investment accounts"


def test_transfer_cross_currency_422(
    client: TestClient,
    bank_account: Account,
    seeded_user: User,
    session: Session,
) -> None:
    """INR source + USD dest → 422. USD accounts are reachable (no INR-only
    guard on AccountCreate), so this is a real boundary check."""
    usd = Account(
        user_id=seeded_user.id,
        name="US Brokerage Cash",
        type="bank",
        issuer=None,
        last4=None,
        currency="USD",
    )
    session.add(usd)
    session.commit()
    session.refresh(usd)

    resp = client.post(
        "/api/v1/transactions/transfer",
        json=_transfer_body(source_account_id=bank_account.id, dest_account_id=usd.id),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "transfer accounts must share a currency"


def _seed_usd_account(session: Session, user_id: UUID, name: str) -> Account:
    """A USD account built via the model — bypasses AccountCreate's INR-only
    gate to exercise the transaction-boundary guards (defense in depth)."""
    acct = Account(
        user_id=user_id,
        name=name,
        type="bank",
        issuer=None,
        last4=None,
        currency="USD",
    )
    session.add(acct)
    session.commit()
    session.refresh(acct)
    return acct


def test_create_spend_on_usd_account_422(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """v1 spending is INR-only — a spend posted to a (model-seeded) USD account
    is rejected before it can be summed as INR paise by the dashboards."""
    usd = _seed_usd_account(session, seeded_user.id, "US Cash")
    resp = client.post("/api/v1/transactions", json=_post_body(account_id=usd.id))
    assert resp.status_code == 422
    assert resp.json()["detail"] == "spending transactions must be on an INR account"


def test_create_income_on_usd_account_422(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """Same gate applies to income (not just spend/refund)."""
    usd = _seed_usd_account(session, seeded_user.id, "US Cash")
    resp = client.post(
        "/api/v1/transactions",
        json=_post_body(account_id=usd.id, amount_paise=100000, transaction_type="income"),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "spending transactions must be on an INR account"


def test_transfer_usd_to_usd_422(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """A same-currency USD↔USD transfer passes the cross-currency check but is
    still rejected — v1 money movement is INR-only."""
    src = _seed_usd_account(session, seeded_user.id, "US Cash A")
    dst = _seed_usd_account(session, seeded_user.id, "US Cash B")
    resp = client.post(
        "/api/v1/transactions/transfer",
        json=_transfer_body(source_account_id=src.id, dest_account_id=dst.id),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "transfers must be on INR accounts"


def test_transfer_dup_dest_leg_409_and_source_rolled_back(
    client: TestClient,
    bank_account: Account,
    axis_account: Account,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """A pre-existing row matching the DEST leg's fingerprint → 409, and the
    rollback discards the already-inserted SOURCE leg (no orphan).

    Dest-leg-specifically because ``add_all([source, dest])`` flushes the source
    INSERT first; the collision fires on the dest INSERT, so this exercises the
    'roll back the source leg too' path.
    """
    dest_fp = transaction_fingerprint(
        txn_date=date(2026, 5, 24),
        amount_paise=500000,
        normalized_merchant=normalize_merchant("Transfer from HDFC Bank"),
        account_id=axis_account.id,
    )
    existing = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 24),
        amount_paise=500000,
        fingerprint=dest_fp,
        transaction_type="transfer",
        merchant_raw="Transfer from HDFC Bank",
    )
    session.add(existing)
    session.commit()
    existing_id = existing.id

    resp = client.post(
        "/api/v1/transactions/transfer",
        json=_transfer_body(source_account_id=bank_account.id, dest_account_id=axis_account.id),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "transaction already exists"

    with session_factory() as s:
        # The failed transfer left NO row on the source (bank) account.
        src_rows = s.scalars(
            select(Transaction).where(Transaction.account_id == bank_account.id)
        ).all()
        assert src_rows == []
        # The dest account still has only the pre-seeded row.
        dst_rows = s.scalars(
            select(Transaction).where(Transaction.account_id == axis_account.id)
        ).all()
        assert [row.id for row in dst_rows] == [existing_id]


def test_transfer_source_leg_collision_409(
    client: TestClient,
    bank_account: Account,
    axis_account: Account,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """A pre-existing row matching the SOURCE leg's fingerprint → 409.

    Mirror of the dest-leg case: ``add_all([source, dest])`` flushes the source
    INSERT first, so a source collision aborts flush #1 before the dest INSERT
    is attempted — the more common real-world re-entry (re-logging the outflow
    you remember). Neither leg should persist.
    """
    source_fp = transaction_fingerprint(
        txn_date=date(2026, 5, 24),
        amount_paise=-500000,
        normalized_merchant=normalize_merchant("Transfer to Axis CC"),
        account_id=bank_account.id,
    )
    existing = _make_txn(
        user_id=bank_account.user_id,
        account_id=bank_account.id,
        txn_date=date(2026, 5, 24),
        amount_paise=-500000,
        fingerprint=source_fp,
        transaction_type="transfer",
        merchant_raw="Transfer to Axis CC",
    )
    session.add(existing)
    session.commit()
    existing_id = existing.id

    resp = client.post(
        "/api/v1/transactions/transfer",
        json=_transfer_body(source_account_id=bank_account.id, dest_account_id=axis_account.id),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "transaction already exists"

    with session_factory() as s:
        # No row persisted on the dest (axis) account.
        dst_rows = s.scalars(
            select(Transaction).where(Transaction.account_id == axis_account.id)
        ).all()
        assert dst_rows == []
        # Source account still has only the pre-seeded row.
        src_rows = s.scalars(
            select(Transaction).where(Transaction.account_id == bank_account.id)
        ).all()
        assert [row.id for row in src_rows] == [existing_id]


def test_transfer_non_fingerprint_integrity_error_propagates(
    client: TestClient,
    bank_account: Account,
    axis_account: Account,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-fingerprint IntegrityError must propagate (→ 500), not be masked as
    409. Fault-injection: make the flush of the two pending transfer legs raise
    an IntegrityError whose ``orig`` text doesn't match the fingerprint
    heuristic; the route's ``raise`` branch must re-raise rather than 409.

    Mirrors ``test_post_non_fingerprint_integrity_error_propagates`` but patches
    ``flush`` (not ``commit``) because the transfer route flushes explicitly to
    assign ids before cross-linking.
    """
    from sqlalchemy.orm import Session as SaSession

    class _FakeOrig(Exception):
        def __str__(self) -> str:
            return "FOREIGN KEY constraint failed"

    real_flush = SaSession.flush

    def boom(self: SaSession, *args: object, **kwargs: object) -> None:
        # Only break the flush that carries the pending transfer legs.
        if any(isinstance(obj, Transaction) for obj in self.new):
            raise IntegrityError("INSERT INTO transactions", {}, _FakeOrig())
        real_flush(self, *args, **kwargs)

    monkeypatch.setattr(SaSession, "flush", boom)

    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        client.post(
            "/api/v1/transactions/transfer",
            json=_transfer_body(source_account_id=bank_account.id, dest_account_id=axis_account.id),
        )


def test_transfer_then_delete_one_leg_unlinks_partner(
    client: TestClient,
    bank_account: Account,
    axis_account: Account,
    session_factory: sessionmaker[Session],
) -> None:
    """Interop with the existing DELETE pre-flight: a transfer created via the
    endpoint, then DELETE one leg, nulls the partner's transfer_pair_id."""
    resp = client.post(
        "/api/v1/transactions/transfer",
        json=_transfer_body(source_account_id=bank_account.id, dest_account_id=axis_account.id),
    )
    assert resp.status_code == 201
    body = resp.json()
    source_id = body["source"]["id"]
    dest_id = body["dest"]["id"]

    delete_resp = client.delete(f"/api/v1/transactions/{source_id}")
    assert delete_resp.status_code == 204, delete_resp.text

    with session_factory() as s:
        assert s.scalar(select(Transaction).where(Transaction.id == source_id)) is None
        partner = s.scalar(select(Transaction).where(Transaction.id == dest_id))
        assert partner is not None
        assert partner.transfer_pair_id is None
        assert partner.transaction_type == "transfer"


# ---------------------------------------------------------------------------
# POST /transactions/{id}/unlink (PRD §F4a-1 break-the-link)
# ---------------------------------------------------------------------------


def test_unlink_clears_both_legs(
    client: TestClient,
    axis_account: Account,
    bank_account: Account,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Unlinking one leg clears transfer_pair_id on BOTH; both survive as transfers."""
    cc_row = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=500000,
        fingerprint="fp-unlink-cc",
        transaction_type="transfer",
    )
    session.add(cc_row)
    session.flush()
    bank_row = _make_txn(
        user_id=bank_account.user_id,
        account_id=bank_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-500000,
        fingerprint="fp-unlink-bank",
        transaction_type="transfer",
    )
    session.add(bank_row)
    session.flush()
    cc_row.transfer_pair_id = bank_row.id
    bank_row.transfer_pair_id = cc_row.id
    session.commit()
    cc_id, bank_id = cc_row.id, bank_row.id

    resp = client.post(f"/api/v1/transactions/{cc_id}/unlink")
    assert resp.status_code == 204, resp.text

    with session_factory() as s:
        cc_after = s.scalar(select(Transaction).where(Transaction.id == cc_id))
        bank_after = s.scalar(select(Transaction).where(Transaction.id == bank_id))
        assert cc_after is not None and bank_after is not None
        assert cc_after.transfer_pair_id is None
        assert bank_after.transfer_pair_id is None
        # Type stays 'transfer' — no provenance to restore.
        assert cc_after.transaction_type == "transfer"
        assert bank_after.transaction_type == "transfer"

    # Re-unlinking the freshly-orphaned partner is a clean 204 no-op — it was
    # paired moments ago, so this pins the no-op path on a just-cleared row
    # (distinct from test_unlink_idempotent_on_unpaired's never-paired row).
    assert client.post(f"/api/v1/transactions/{bank_id}/unlink").status_code == 204

    # Orphan transfers stay board-listable under the transfer filter
    # (they keep confirmed_at; confirmed_only doesn't filter type).
    listed_ids = {
        row["id"] for row in client.get("/api/v1/transactions?transaction_type=transfer").json()
    }
    assert {cc_id, bank_id} <= listed_ids


def test_unlink_idempotent_on_unpaired(
    client: TestClient,
    axis_account: Account,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Unlinking a row with no transfer_pair_id is a 204 no-op (retryable)."""
    # A transfer-typed row that was never paired — the realistic already-unlinked
    # target (not the _make_txn default 'spend').
    row = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 5, 10),
        amount_paise=500000,
        fingerprint="fp-unlink-solo",
        transaction_type="transfer",
    )
    session.add(row)
    session.commit()
    row_id = row.id

    resp = client.post(f"/api/v1/transactions/{row_id}/unlink")
    assert resp.status_code == 204, resp.text

    with session_factory() as s:
        after = s.scalar(select(Transaction).where(Transaction.id == row_id))
        assert after is not None
        assert after.transfer_pair_id is None
        assert after.transaction_type == "transfer"


def test_unlink_unknown_id_404(
    client: TestClient,
) -> None:
    resp = client.post("/api/v1/transactions/99999/unlink")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "transaction not found"


def test_unlink_foreign_user_404(
    client: TestClient,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """A paired pair owned by ANOTHER user → 404, and the foreign pair is left
    UNCHANGED — the ownership check must precede any mutation."""
    other = User(id=uuid4())
    session.add(other)
    session.flush()
    foreign_acct = Account(user_id=other.id, name="Foreign", type="bank", issuer=None, last4=None)
    session.add(foreign_acct)
    session.flush()
    a = _make_txn(
        user_id=other.id,
        account_id=foreign_acct.id,
        txn_date=date(2026, 5, 10),
        amount_paise=500000,
        fingerprint="fp-foreign-a",
        transaction_type="transfer",
    )
    session.add(a)
    session.flush()
    b = _make_txn(
        user_id=other.id,
        account_id=foreign_acct.id,
        txn_date=date(2026, 5, 10),
        amount_paise=-500000,
        fingerprint="fp-foreign-b",
        transaction_type="transfer",
    )
    session.add(b)
    session.flush()
    a.transfer_pair_id = b.id
    b.transfer_pair_id = a.id
    session.commit()
    a_id, b_id = a.id, b.id

    resp = client.post(f"/api/v1/transactions/{a_id}/unlink")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "transaction not found"

    with session_factory() as s:
        a_after = s.scalar(select(Transaction).where(Transaction.id == a_id))
        b_after = s.scalar(select(Transaction).where(Transaction.id == b_id))
        assert a_after is not None and b_after is not None
        # Foreign pair untouched — unlink mutated nothing before the 404.
        assert a_after.transfer_pair_id == b_id
        assert b_after.transfer_pair_id == a_id


def test_transfer_then_unlink_end_to_end(
    client: TestClient,
    bank_account: Account,
    axis_account: Account,
    session_factory: sessionmaker[Session],
) -> None:
    """Create a pair via the real F2 writer, then unlink one leg → both cleared."""
    resp = client.post(
        "/api/v1/transactions/transfer",
        json=_transfer_body(source_account_id=bank_account.id, dest_account_id=axis_account.id),
    )
    assert resp.status_code == 201
    body = resp.json()
    source_id = body["source"]["id"]
    dest_id = body["dest"]["id"]

    unlink_resp = client.post(f"/api/v1/transactions/{source_id}/unlink")
    assert unlink_resp.status_code == 204, unlink_resp.text

    with session_factory() as s:
        src = s.scalar(select(Transaction).where(Transaction.id == source_id))
        dst = s.scalar(select(Transaction).where(Transaction.id == dest_id))
        assert src is not None and dst is not None
        assert src.transfer_pair_id is None
        assert dst.transfer_pair_id is None
        assert src.transaction_type == "transfer"
        assert dst.transaction_type == "transfer"


# --- ADR-0007: the widened PATCH mutable set -----------------------------------------


def _fp_of(txn: Transaction) -> str:
    """The PRD §F4 fingerprint the row's CURRENT columns should hash to."""
    return transaction_fingerprint(
        txn_date=txn.date,
        amount_paise=txn.amount_paise,
        normalized_merchant=txn.merchant_normalized,
        account_id=txn.account_id,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("date", "2026-06-01"),
        ("amount_paise", -99999),
        ("merchant_raw", "Corrected Merchant"),
    ],
)
def test_patch_identity_field_recomputes_the_fingerprint(
    client: TestClient,
    axis_account: Account,
    session: Session,
    field: str,
    value: object,
) -> None:
    """Rule 3: each identity input recomputes the hash and re-enters the F4 contract."""
    txn = _seed_one(session, axis_account=axis_account)
    before = txn.fingerprint

    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={field: value})
    assert resp.status_code == 200, resp.text

    session.refresh(txn)
    assert txn.fingerprint != before
    assert txn.fingerprint == _fp_of(txn)
    assert txn.occurrence == 0


def test_patch_account_id_recomputes_the_fingerprint(
    client: TestClient,
    axis_account: Account,
    bank_account: Account,
    session: Session,
) -> None:
    """The fourth identity input — account_id is hashed too (PRD §F4)."""
    txn = _seed_one(session, axis_account=axis_account)
    before = txn.fingerprint

    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"account_id": bank_account.id})
    assert resp.status_code == 200, resp.text

    session.refresh(txn)
    assert txn.account_id == bank_account.id
    assert txn.fingerprint != before
    assert txn.fingerprint == _fp_of(txn)


def test_patch_merchant_raw_recomputes_merchant_normalized(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """merchant_normalized is derived, never accepted from the body (rule 1)."""
    txn = _seed_one(session, axis_account=axis_account)

    resp = client.patch(
        f"/api/v1/transactions/{txn.id}", json={"merchant_raw": "  SWIGGY   Bangalore "}
    )
    assert resp.status_code == 200, resp.text

    session.refresh(txn)
    assert txn.merchant_raw == "SWIGGY   Bangalore"  # stripped, not normalized
    assert txn.merchant_normalized == normalize_merchant("SWIGGY   Bangalore")
    assert txn.fingerprint == _fp_of(txn)


def test_patch_clearing_merchant_hashes_like_a_row_created_without_one(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """The create path's ``None`` → ``""`` convention, applied on PATCH.

    A stored ``""`` would take a different branch than a stored NULL, so a merchant
    cleared here must land in exactly the state a blank one lands in on POST.
    """
    txn = _seed_one(session, axis_account=axis_account)

    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"merchant_raw": "   "})
    assert resp.status_code == 200, resp.text

    session.refresh(txn)
    assert txn.merchant_raw is None
    assert txn.merchant_normalized == ""
    assert txn.fingerprint == transaction_fingerprint(
        txn_date=txn.date,
        amount_paise=txn.amount_paise,
        normalized_merchant="",
        account_id=txn.account_id,
    )


def test_patch_identity_field_to_the_same_value_is_a_no_op(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Rule 2: the route branches on whether an identity input ACTUALLY changed.

    Re-sending the stored value must not recompute, must not reset ``occurrence``,
    and must not 409 against the row's own fingerprint.
    """
    txn = _seed_one(session, axis_account=axis_account)
    txn.occurrence = 3
    txn.fingerprint = _fp_of(txn)
    session.commit()
    before = txn.fingerprint

    resp = client.patch(
        f"/api/v1/transactions/{txn.id}",
        json={"amount_paise": txn.amount_paise, "date": txn.date.isoformat()},
    )
    assert resp.status_code == 200, resp.text

    session.refresh(txn)
    assert txn.fingerprint == before
    assert txn.occurrence == 3  # not vacated


def test_patch_cosmetic_merchant_recasing_keeps_the_occurrence(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """``occurrence`` resets only when the FINGERPRINT actually moved.

    ``normalize_merchant`` lowercases and collapses whitespace, so re-casing
    ``merchant_raw`` changes a user-visible column without changing identity. Keying
    the reset on the recomputed hash rather than on the raw input is what stops a
    tidy-up inside a duplicate group from vacating occurrence 1 onto the occupied 0
    and 409-ing on its own twin.
    """
    txn = _seed_one(session, axis_account=axis_account)
    txn.merchant_raw = "UBER INDIA"
    txn.merchant_normalized = normalize_merchant("UBER INDIA")
    txn.fingerprint = _fp_of(txn)
    txn.occurrence = 1
    session.commit()
    # The twin at occurrence 0 that a spurious reset would collide with.
    twin = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=txn.date,
        amount_paise=txn.amount_paise,
        fingerprint=txn.fingerprint,
        merchant_raw="UBER INDIA",
    )
    session.add(twin)
    session.commit()

    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"merchant_raw": "Uber India"})
    assert resp.status_code == 200, resp.text

    session.refresh(txn)
    assert txn.merchant_raw == "Uber India"
    assert txn.occurrence == 1


def test_patch_amount_into_an_existing_fingerprint_409s(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """ADR-0007 §Verification 3: edit into a collision → 409, not a silent duplicate."""
    victim = _seed_one(session, axis_account=axis_account, fingerprint="fp-a")
    other = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=victim.date,
        amount_paise=-77777,
        fingerprint="fp-b",
        merchant_raw=victim.merchant_raw or "TEST MERCHANT",
    )
    other.fingerprint = _fp_of(other)
    session.add(other)
    session.commit()

    resp = client.patch(f"/api/v1/transactions/{victim.id}", json={"amount_paise": -77777})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "transaction already exists"


def test_patch_colliding_amount_with_a_label_change_409s_not_500(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """The 409 must survive the label/learning SAVEPOINTs.

    ``set_labels_on_transaction`` and ``learn_merchant_memory`` open
    ``begin_nested()`` SAVEPOINTs whose entry-flush emits any pending UPDATE. If the
    dedup conflict were left to surface at ``session.commit()``, that flush would
    raise it inside a savepoint whose conflict predicate is the LABEL/TAG one — which
    re-raises past the route's handler as a 500. The route flushes the column edits
    itself, inside its own guard, exactly to keep this a 409.
    """
    victim = _seed_one(session, axis_account=axis_account, fingerprint="fp-a")
    other = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=victim.date,
        amount_paise=-77777,
        fingerprint="fp-b",
        merchant_raw=victim.merchant_raw or "TEST MERCHANT",
    )
    other.fingerprint = _fp_of(other)
    session.add(other)
    session.commit()

    resp = client.patch(
        f"/api/v1/transactions/{victim.id}",
        json={"amount_paise": -77777, "labels": ["reimbursable"]},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "transaction already exists"


def test_patch_income_to_refund_with_a_refund_category(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """An unmatched credit imports as income. Changing to refund requires a refund category."""
    txn = _seed_one(session, axis_account=axis_account)
    txn.transaction_type = "income"
    txn.amount_paise = 45000
    txn.category_id = _seed_category(session, axis_account.user_id, "Cashback", "income").id
    txn.fingerprint = _fp_of(txn)
    session.commit()
    refund_cat = _seed_category(session, axis_account.user_id, "Refund", "refund")

    resp = client.patch(
        f"/api/v1/transactions/{txn.id}",
        json={"transaction_type": "refund", "category_id": refund_cat.id},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["transaction_type"] == "refund"
    assert resp.json()["category_id"] == refund_cat.id

    session.refresh(txn)
    assert txn.fingerprint == _fp_of(txn)


def test_patch_type_kind_flip_without_a_compatible_category_422s(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Rule 5: the kind follows the POST-patch type, and clearing is never silent.

    Alternative 5 was rejected — the picker already knows the required kind, so one
    round-trip covers it, and auto-clearing would destroy a choice the user made.
    """
    txn = _seed_one(session, axis_account=axis_account)
    txn.transaction_type = "income"
    txn.amount_paise = 45000
    txn.category_id = _seed_category(session, axis_account.user_id, "Cashback", "income").id
    session.commit()

    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"transaction_type": "refund"})
    assert resp.status_code == 422, resp.text
    assert "compatible category_id" in resp.json()["detail"]

    session.refresh(txn)
    assert txn.transaction_type == "income"  # rejected before the first setattr


def test_patch_type_kind_flip_with_an_explicit_null_category_succeeds(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """The explicit-null escape hatch rule 5 names."""
    txn = _seed_one(session, axis_account=axis_account)
    txn.transaction_type = "income"
    txn.amount_paise = 45000
    txn.category_id = _seed_category(session, axis_account.user_id, "Cashback", "income").id
    session.commit()

    resp = client.patch(
        f"/api/v1/transactions/{txn.id}",
        json={"transaction_type": "refund", "category_id": None},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["category_id"] is None


def test_patch_type_kind_flip_with_no_category_at_all_succeeds(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Nothing to strand — an uncategorized row flips kind freely."""
    txn = _seed_one(session, axis_account=axis_account)
    txn.transaction_type = "income"
    txn.amount_paise = 45000
    session.commit()

    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"transaction_type": "refund"})
    assert resp.status_code == 200, resp.text


def test_patch_same_kind_type_change_keeps_the_category(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Changing fields within the same type keeps the category."""
    txn = _seed_one(session, axis_account=axis_account)
    food = _seed_category(session, axis_account.user_id, "Food", "spend")
    txn.category_id = food.id
    session.commit()

    resp = client.patch(
        f"/api/v1/transactions/{txn.id}",
        json={"amount_paise": -9999},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["category_id"] == food.id


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"transaction_type": "income"}, "income requires positive amount_paise"),
        ({"amount_paise": 500}, "spend requires negative amount_paise"),
        ({"amount_paise": 0}, "amount_paise must be non-zero"),
    ],
)
def test_patch_sign_and_type_are_validated_as_a_merged_pair(
    client: TestClient,
    axis_account: Account,
    session: Session,
    body: dict[str, object],
    expected: str,
) -> None:
    """Rule 4 — the F2 sign rule applied to the MERGED state.

    A schema validator cannot see the stored row, so each of these is legal in
    isolation and wrong once merged with what is already there.
    """
    txn = _seed_one(session, axis_account=axis_account)  # spend, -12345

    resp = client.patch(f"/api/v1/transactions/{txn.id}", json=body)
    assert resp.status_code == 422, resp.text
    assert expected in str(resp.json()["detail"])


def test_patch_type_and_amount_together_can_flip_the_sign(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """The merged pair is what is checked, so one request can legally do both."""
    txn = _seed_one(session, axis_account=axis_account)

    resp = client.patch(
        f"/api/v1/transactions/{txn.id}",
        json={"transaction_type": "income", "amount_paise": 45000},
    )
    assert resp.status_code == 200, resp.text


def test_patch_transaction_type_to_transfer_422s(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Rule 7: transfer is not a valid PATCH TARGET.

    Pairs are born via ``POST /transactions/transfer``; a lone leg minted here would
    have no second leg and would violate ADR-0002's exactly-two-pairing invariant.
    """
    txn = _seed_one(session, axis_account=axis_account)

    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"transaction_type": "transfer"})
    assert resp.status_code == 422, resp.text
    assert "POST /transactions/transfer" in resp.json()["detail"]


def _live_pair(client: TestClient, *, source_id: int, dest_id: int) -> dict[str, object]:
    resp = client.post(
        "/api/v1/transactions/transfer",
        json=_transfer_body(source_account_id=source_id, dest_account_id=dest_id),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.parametrize(
    "body",
    [
        {"amount_paise": -600000},
        {"date": "2026-06-02"},
        {"merchant_raw": "Corrected"},
        {"transaction_type": "spend", "amount_paise": -600000},
    ],
)
def test_patch_a_live_transfer_leg_422s(
    client: TestClient,
    bank_account: Account,
    axis_account: Account,
    body: dict[str, object],
) -> None:
    """Rule 7: a paired row rejects identity and type edits — unlink first.

    The two legs are one money movement with server-derived signs; editing one alone
    breaks ADR-0002's symmetry invariant (Alternative 6, rejected).
    """
    pair = _live_pair(client, source_id=bank_account.id, dest_id=axis_account.id)
    leg_id = pair["source"]["id"]

    resp = client.patch(f"/api/v1/transactions/{leg_id}", json=body)
    assert resp.status_code == 422, resp.text
    assert "unlink" in resp.json()["detail"]


def test_patch_a_live_transfer_legs_category_and_labels_still_work(
    client: TestClient,
    bank_account: Account,
    axis_account: Account,
    session: Session,
) -> None:
    """Rule 7 keeps ``category_id`` AND ``labels`` editable on a paired row.

    Neither participates in the pairing, and a transfer legitimately carries a spend
    category — which is what lets the F4a banner offer a relabel without unlinking.
    """
    pair = _live_pair(client, source_id=bank_account.id, dest_id=axis_account.id)
    leg_id = pair["source"]["id"]
    food = _seed_category(session, bank_account.user_id, "Food", "spend")

    resp = client.patch(
        f"/api/v1/transactions/{leg_id}",
        json={"category_id": food.id, "labels": ["cc-bill"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["category_id"] == food.id
    assert [lbl["name"] for lbl in resp.json()["labels"]] == ["cc-bill"]


def test_patch_an_unlinked_transfer_leg_is_editable(
    client: TestClient,
    bank_account: Account,
    axis_account: Account,
) -> None:
    """Unpaired transfers are legal (survivors of delete/unlink) and fully editable.

    Rule 7 forbids *creating* one via PATCH, not correcting one that already exists.
    """
    pair = _live_pair(client, source_id=bank_account.id, dest_id=axis_account.id)
    leg_id = pair["source"]["id"]
    assert client.post(f"/api/v1/transactions/{leg_id}/unlink").status_code == 204

    resp = client.patch(f"/api/v1/transactions/{leg_id}", json={"amount_paise": -600000})
    assert resp.status_code == 200, resp.text


def test_patch_account_id_cross_user_422s(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Rule 6 + tenant rule 3: a body FK pointing at another user's row is 422."""
    txn = _seed_one(session, axis_account=axis_account)
    other = User(id=uuid4())
    session.add(other)
    session.flush()
    foreign = Account(user_id=other.id, name="Their Card", type="credit_card", last4="9999")
    session.add(foreign)
    session.commit()
    session.refresh(foreign)

    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"account_id": foreign.id})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "account not found or archived"

    session.refresh(txn)
    assert txn.account_id == axis_account.id


def test_patch_account_id_archived_422s(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Rule 6, check 2 — the same archived-account refusal the create path makes."""
    txn = _seed_one(session, axis_account=axis_account)
    archived = Account(
        user_id=axis_account.user_id,
        name="Old Card",
        type="credit_card",
        last4="0000",
        archived_at=datetime.now(UTC),
    )
    session.add(archived)
    session.commit()
    session.refresh(archived)

    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"account_id": archived.id})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "account not found or archived"


def test_patch_account_id_investment_422s(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Rule 6, check 3 — F7 owns investment txns on a separate table."""
    txn = _seed_one(session, axis_account=axis_account)
    demat = Account(user_id=axis_account.user_id, name="Zerodha", type="investment")
    session.add(demat)
    session.commit()
    session.refresh(demat)

    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"account_id": demat.id})
    assert resp.status_code == 422, resp.text
    assert "investment" in resp.json()["detail"]


def test_patch_account_id_non_inr_422s(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Rule 6, check 4 — the currency-blind F8 aggregates would sum cents as paise."""
    txn = _seed_one(session, axis_account=axis_account)
    usd = _seed_usd_account(session, axis_account.user_id, "Chase")

    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"account_id": usd.id})
    assert resp.status_code == 422, resp.text
    assert "INR" in resp.json()["detail"]


@pytest.mark.parametrize("field", ["date", "amount_paise", "account_id", "transaction_type"])
def test_patch_explicit_null_on_a_not_null_column_422s(
    client: TestClient,
    axis_account: Account,
    session: Session,
    field: str,
) -> None:
    """ "Unset the date" has no meaning — and a silent no-op would read as success."""
    txn = _seed_one(session, axis_account=axis_account)

    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={field: None})
    assert resp.status_code == 422, resp.text


def test_patch_a_pending_row_identity_field(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Rule 10: no ``confirmed_at`` gate — the queue is a confirmation gate, not a lock."""
    txn = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 4, 10),
        amount_paise=-12345,
        fingerprint="fp-pending",
        confirmed_at=None,
    )
    session.add(txn)
    session.commit()

    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"amount_paise": -13000})
    assert resp.status_code == 200, resp.text

    session.refresh(txn)
    assert txn.confirmed_at is None  # still pending
    assert txn.fingerprint == _fp_of(txn)


def test_patch_never_touches_origin_fingerprint(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Rule 9 through the route: provenance is written once, by the importer.

    This is what stops the edit re-staging its own pre-edit row on the next upload
    of the same statement.
    """
    txn = _seed_one(session, axis_account=axis_account)
    txn.fingerprint = _fp_of(txn)
    txn.origin_fingerprint = txn.fingerprint
    session.commit()
    stamped = txn.origin_fingerprint

    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"amount_paise": -55555})
    assert resp.status_code == 200, resp.text

    session.refresh(txn)
    assert txn.origin_fingerprint == stamped
    assert txn.fingerprint != stamped


def test_patch_identity_edit_leaves_auto_category_id_frozen(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Rule 8: ``auto_category_id`` is the import-time suggestion the metric reads."""
    txn = _seed_one(session, axis_account=axis_account)
    suggested = _seed_category(session, axis_account.user_id, "Food", "spend")
    txn.auto_category_id = suggested.id
    txn.category_id = suggested.id
    session.commit()

    resp = client.patch(
        f"/api/v1/transactions/{txn.id}",
        json={"merchant_raw": "Different Merchant", "amount_paise": -22222},
    )
    assert resp.status_code == 200, resp.text

    session.refresh(txn)
    assert txn.auto_category_id == suggested.id


def test_patch_merchant_rename_with_a_category_change_teaches_the_new_merchant(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Rule 8's accepted consequence, made explicit.

    The learn block reads ``merchant_normalized`` AFTER the setattr loop, so a rename
    plus a category change in one request teaches the corrected merchant — the right
    key — and the old merchant's rule is left alone (no decay in v1).
    """
    txn = _seed_one(session, axis_account=axis_account)
    food = _seed_category(session, axis_account.user_id, "Food", "spend")

    resp = client.patch(
        f"/api/v1/transactions/{txn.id}",
        json={"merchant_raw": "SWIGGY", "category_id": food.id},
    )
    assert resp.status_code == 200, resp.text

    rules = session.scalars(select(MerchantTagMap)).all()
    assert [(r.merchant_normalized, r.category_id) for r in rules] == [("swiggy", food.id)]


def test_patch_retyping_a_pending_row_to_income_does_not_learn(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """The learning gate reads the POST-patch type (rule 8) and the pending gate holds.

    Belt and braces on both: income never teaches, and a pending row learns only at
    import commit (ADR-0004).
    """
    txn = _make_txn(
        user_id=axis_account.user_id,
        account_id=axis_account.id,
        txn_date=date(2026, 4, 10),
        amount_paise=-12345,
        fingerprint="fp-pending-learn",
        confirmed_at=None,
    )
    session.add(txn)
    session.commit()
    income_cat = _seed_category(session, axis_account.user_id, "Cashback", "income")

    resp = client.patch(
        f"/api/v1/transactions/{txn.id}",
        json={"transaction_type": "income", "amount_paise": 12345, "category_id": income_cat.id},
    )
    assert resp.status_code == 200, resp.text
    assert session.scalars(select(MerchantTagMap)).all() == []


def test_patch_labels_only_does_not_revalidate_an_untouched_sign(
    client: TestClient,
    axis_account: Account,
    session: Session,
) -> None:
    """Rule 4 fires only when the request puts the sign/type pair in play.

    ``backup_csv`` validates the type vocabulary and that ``amount_paise`` parses,
    but not the sign pairing — and a hand-edited zip is its declared threat model —
    so a stored ``refund`` carrying a negative amount is reachable. A labels-only
    PATCH must not 422 on state it never touched.
    """
    txn = _seed_one(session, axis_account=axis_account)
    txn.transaction_type = "refund"  # negative amount: violates the F2 rule
    session.commit()

    resp = client.patch(f"/api/v1/transactions/{txn.id}", json={"labels": ["disputed"]})
    assert resp.status_code == 200, resp.text
    assert [lbl["name"] for lbl in resp.json()["labels"]] == ["disputed"]

    # But touching either half of the pair does re-validate it.
    still_bad = client.patch(f"/api/v1/transactions/{txn.id}", json={"amount_paise": -20000})
    assert still_bad.status_code == 422
    assert "refund requires positive amount_paise" in str(still_bad.json()["detail"])
