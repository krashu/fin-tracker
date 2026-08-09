"""End-to-end tests for the full ``/api/v1/accounts`` CRUD (PRD §F6).

Covers POST (balance-by-type validator, the partial unique index on
``(user_id, name) WHERE archived_at IS NULL``, 409 on duplicate active
name), GET (active-only filter, name-ordering, cross-user isolation),
PATCH (rename / issuer / last4 / parent_account_id with 5-rule
validation, locked-field rejection, idempotency short-circuit, 409 on
rename collision), and DELETE (soft-delete via ``archived_at``,
idempotent 404 on re-DELETE, partial-unique-index allows re-create with
the same name after archive).
"""

from __future__ import annotations

from datetime import datetime
from typing import get_args
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Account, User
from app.models.account import AccountTypeStr
from app.schemas import NET_WORTH_EXCLUDED_TYPES


def _post(client: TestClient, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    resp = client.post("/api/v1/accounts", json=payload)
    return resp.status_code, resp.json()


def _seed_account(
    session: Session,
    user_id: UUID,
    *,
    name: str,
    type: AccountTypeStr = "credit_card",
    issuer: str | None = "axis",
    last4: str | None = None,
    opening_balance_paise: int = 0,
    archived_at: datetime | None = None,
) -> Account:
    account = Account(
        user_id=user_id,
        name=name,
        type=type,
        issuer=issuer,
        last4=last4,
        opening_balance_paise=opening_balance_paise,
        archived_at=archived_at,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def test_create_usd_account_422(
    client: TestClient,
    seeded_user: User,
    session_factory: sessionmaker[Session],
) -> None:
    """v1 spending is INR-only — AccountCreate rejects a non-INR account (any
    type) and persists nothing."""
    status_code, body = _post(client, {"name": "US Cash", "type": "bank", "currency": "USD"})
    assert status_code == 422
    assert "INR" in str(body["detail"])
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Account)) == 0


def test_create_account_minimal(
    client: TestClient,
    seeded_user: User,
    session_factory: sessionmaker[Session],
) -> None:
    # A credit_card requires a supported issuer (parser-dispatch key), so the
    # minimal valid body carries one. See test_create_bank_no_issuer for the
    # truly issuer-less minimal case (non-CC).
    status_code, body = _post(client, {"name": "Axis CC", "type": "credit_card", "issuer": "axis"})

    assert status_code == 201
    assert body["id"] >= 1
    assert body["name"] == "Axis CC"
    assert body["type"] == "credit_card"
    assert body["issuer"] == "axis"
    assert body["last4"] is None
    assert body["opening_balance_paise"] == 0
    assert body["currency"] == "INR"
    assert body["parent_account_id"] is None
    assert body["archived_at"] is None

    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Account)) == 1


def test_create_account_full(
    client: TestClient,
    seeded_user: User,
) -> None:
    status_code, body = _post(
        client,
        {
            "name": "Axis Flipkart",
            "type": "credit_card",
            "issuer": "axis",
            "last4": "1234",
            "opening_balance_paise": -50000,
            "currency": "INR",
        },
    )

    assert status_code == 201
    assert body["name"] == "Axis Flipkart"
    assert body["issuer"] == "axis"
    assert body["last4"] == "1234"
    assert body["opening_balance_paise"] == -50000


def test_create_account_invalid_type_rejected(
    client: TestClient,
    seeded_user: User,
) -> None:
    status_code, _ = _post(client, {"name": "X", "type": "weird"})
    assert status_code == 422


def test_create_account_rejects_extra_fields(
    client: TestClient,
    seeded_user: User,
) -> None:
    """AccountCreate uses extra='forbid' so unknown JSON keys surface as 422."""
    status_code, _ = _post(
        client,
        {"name": "X", "type": "credit_card", "foo": "bar"},
    )
    assert status_code == 422


def test_create_account_invalid_last4_rejected(
    client: TestClient,
    seeded_user: User,
) -> None:
    status_code_short, _ = _post(
        client,
        {"name": "X", "type": "credit_card", "last4": "12"},
    )
    assert status_code_short == 422

    status_code_alpha, _ = _post(
        client,
        {"name": "Y", "type": "credit_card", "last4": "abcd"},
    )
    assert status_code_alpha == 422


def test_create_account_empty_name_rejected(
    client: TestClient,
    seeded_user: User,
) -> None:
    status_code, _ = _post(client, {"name": "", "type": "credit_card"})
    assert status_code == 422


def test_create_account_cc_positive_balance_rejected(
    client: TestClient,
    seeded_user: User,
) -> None:
    """credit_card requires opening_balance_paise <= 0 (debt is negative)."""
    status_code, _ = _post(
        client,
        {"name": "Bad CC", "type": "credit_card", "opening_balance_paise": 50000},
    )
    assert status_code == 422


def test_create_account_bank_negative_balance_rejected(
    client: TestClient,
    seeded_user: User,
) -> None:
    """bank/cash require opening_balance_paise >= 0."""
    status_code, _ = _post(
        client,
        {"name": "Bad Bank", "type": "bank", "opening_balance_paise": -5000},
    )
    assert status_code == 422


@pytest.mark.parametrize("balance", [3_000_000_00, -3_000_000_00])
def test_create_account_investment_nonzero_balance_rejected(
    client: TestClient,
    seeded_user: User,
    session_factory: sessionmaker[Session],
    balance: int,
) -> None:
    """investment requires opening_balance_paise == 0 (B#11, decision 30).

    An investment account is a placeholder — holdings carry the value, so a
    balance recorded here double-counts the portfolio in net worth. Unlike the
    CC / bank rules above this is a *magnitude* rule, not a sign rule: both
    directions are rejected. Nothing is persisted.
    """
    status_code, body = _post(
        client,
        {"name": "Zerodha", "type": "investment", "opening_balance_paise": balance},
    )
    assert status_code == 422
    assert "placeholder" in str(body["detail"])
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Account)) == 0


def test_create_account_investment_zero_balance_accepted(
    client: TestClient,
    seeded_user: User,
) -> None:
    """The rule above must not over-reject: an investment account is still a
    legitimate grouping row, it just carries no money of its own."""
    status_code, body = _post(client, {"name": "Zerodha", "type": "investment"})
    assert status_code == 201
    assert body["type"] == "investment"
    assert body["opening_balance_paise"] == 0


def test_every_account_type_declares_a_net_worth_bucket(
    client: TestClient,
    seeded_user: User,
) -> None:
    """Couple the two halves of the net-worth policy so neither can drift.

    ``NET_WORTH_EXCLUDED_TYPES`` says which types don't count;
    ``AccountCreate._check_balance_by_type`` is what makes excluding
    ``investment`` lossless rather than lossy. Nothing in the type system links
    them, so a fifth account type could join the frozenset with no create-time
    guard and quietly drop real money out of net worth.

    Every ``AccountTypeStr`` member must therefore fall in exactly one bucket,
    and each bucket's claim is exercised, not just declared.
    """
    contributes = {"bank", "cash"}
    excluded_zero_balance = {"investment"}  # excluded AND pinned to 0 at create
    excluded_balance_bearing = {"credit_card"}  # excluded, balance legitimately non-zero

    assert set(get_args(AccountTypeStr)) == (
        contributes | excluded_zero_balance | excluded_balance_bearing
    )
    assert excluded_zero_balance | excluded_balance_bearing == NET_WORTH_EXCLUDED_TYPES

    for account_type in sorted(excluded_zero_balance):
        status_code, _ = _post(
            client,
            {"name": f"Zero {account_type}", "type": account_type, "opening_balance_paise": 1},
        )
        assert status_code == 422, f"{account_type} is excluded but accepts a balance"

    for account_type in sorted(excluded_balance_bearing):
        status_code, _ = _post(
            client,
            {
                "name": f"Bearing {account_type}",
                "type": account_type,
                "issuer": "axis",
                "opening_balance_paise": -50000,
            },
        )
        assert status_code == 201, f"{account_type} should still accept a real balance"


def test_create_account_issuer_lowercased(
    client: TestClient,
    seeded_user: User,
    session_factory: sessionmaker[Session],
) -> None:
    """`Axis` on input → `axis` in DB so import_service.PARSERS dispatch works."""
    status_code, body = _post(
        client,
        {
            "name": "Axis CC mixed",
            "type": "credit_card",
            "issuer": "Axis",
            "last4": "1234",
        },
    )

    assert status_code == 201
    assert body["issuer"] == "axis"

    with session_factory() as s:
        row = s.get(Account, body["id"])
        assert row is not None
        assert row.issuer == "axis"


def test_create_cc_unsupported_issuer_rejected(
    client: TestClient,
    seeded_user: User,
    session_factory: sessionmaker[Session],
) -> None:
    """A credit_card issuer must have a registered parser — else it crashes at
    upload with ParserNotRegisteredError. The route guards it with 422."""
    status_code, body = _post(client, {"name": "HDFC CC", "type": "credit_card", "issuer": "hdfc"})
    assert status_code == 422
    assert "issuer must be one of" in str(body["detail"])
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Account)) == 0


def test_create_cc_missing_issuer_rejected(
    client: TestClient,
    seeded_user: User,
) -> None:
    """issuer is required for credit_card (no default parser)."""
    status_code, body = _post(client, {"name": "Mystery CC", "type": "credit_card"})
    assert status_code == 422
    assert "issuer must be one of" in str(body["detail"])


def test_create_bank_no_issuer(
    client: TestClient,
    seeded_user: User,
) -> None:
    """The guard only applies to credit_card — a bank needs no issuer."""
    status_code, body = _post(client, {"name": "HDFC Savings", "type": "bank"})
    assert status_code == 201
    assert body["issuer"] is None


def test_create_account_duplicate_name_returns_409(
    client: TestClient,
    seeded_user: User,
    session_factory: sessionmaker[Session],
) -> None:
    first_status, _ = _post(client, {"name": "Axis CC", "type": "credit_card", "issuer": "axis"})
    second_status, second_body = _post(client, {"name": "Axis CC", "type": "bank"})

    assert first_status == 201
    assert second_status == 409
    assert second_body["detail"] == "account name already exists"

    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Account)) == 1


def test_create_account_archived_same_name_succeeds(
    client: TestClient,
    seeded_user: User,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Partial unique skips archived rows — the whole reason it's partial."""
    first_status, first_body = _post(
        client, {"name": "Axis CC", "type": "credit_card", "issuer": "axis"}
    )
    assert first_status == 201

    archived = session.get(Account, first_body["id"])
    assert archived is not None
    archived.archived_at = datetime(2026, 5, 24, 12, 0, 0)
    session.commit()

    second_status, _ = _post(client, {"name": "Axis CC", "type": "credit_card", "issuer": "axis"})
    assert second_status == 201

    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Account)) == 2


def test_list_accounts_empty(
    client: TestClient,
    seeded_user: User,
) -> None:
    resp = client.get("/api/v1/accounts")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_accounts_returns_active(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    _seed_account(session, seeded_user.id, name="Axis CC", last4="1234")
    _seed_account(session, seeded_user.id, name="HDFC Bank", type="bank", issuer="hdfc")

    resp = client.get("/api/v1/accounts")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    names = {row["name"] for row in body}
    assert names == {"Axis CC", "HDFC Bank"}


def test_list_accounts_omits_archived(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    _seed_account(session, seeded_user.id, name="Axis CC", last4="1234")
    _seed_account(session, seeded_user.id, name="HDFC Bank", type="bank", issuer="hdfc")
    _seed_account(
        session,
        seeded_user.id,
        name="Old CC",
        last4="9999",
        archived_at=datetime(2026, 1, 1, 12, 0, 0),
    )

    resp = client.get("/api/v1/accounts")
    assert resp.status_code == 200
    names = [row["name"] for row in resp.json()]
    assert names == ["Axis CC", "HDFC Bank"]
    assert "Old CC" not in names


def test_list_accounts_ordered_by_name(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    _seed_account(session, seeded_user.id, name="Zeta", type="bank", issuer="zeta")
    _seed_account(session, seeded_user.id, name="Alpha", type="bank", issuer="alpha")
    _seed_account(session, seeded_user.id, name="Mango", type="bank", issuer="mango")

    resp = client.get("/api/v1/accounts")
    assert resp.status_code == 200
    names = [row["name"] for row in resp.json()]
    assert names == ["Alpha", "Mango", "Zeta"]


def test_list_accounts_response_shape(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    _seed_account(
        session,
        seeded_user.id,
        name="Axis CC",
        last4="1234",
        opening_balance_paise=-50000,
    )

    resp = client.get("/api/v1/accounts")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert set(rows[0].keys()) == {
        "id",
        "name",
        "type",
        "issuer",
        "last4",
        "opening_balance_paise",
        "currency",
        "parent_account_id",
        "archived_at",
    }
    assert "user_id" not in rows[0]


def test_list_accounts_omits_foreign_user_rows(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """A different user's accounts must never appear in this user's list."""
    from uuid import uuid4

    other_user = User(id=uuid4())
    session.add(other_user)
    session.flush()
    # Seed one account each — the current user's must come back, the
    # foreign user's must not.
    _seed_account(session, seeded_user.id, name="Mine", last4="1111")
    foreign = Account(
        user_id=other_user.id,
        name="NotMine",
        type="credit_card",
        issuer="axis",
        last4="2222",
    )
    session.add(foreign)
    session.commit()

    resp = client.get("/api/v1/accounts")
    assert resp.status_code == 200
    names = {row["name"] for row in resp.json()}
    assert names == {"Mine"}
    assert "NotMine" not in names


# ---------- PATCH ----------------------------------------------------------


def _patch(
    client: TestClient, account_id: int, payload: dict[str, object]
) -> tuple[int, dict[str, object]]:
    resp = client.patch(f"/api/v1/accounts/{account_id}", json=payload)
    return resp.status_code, resp.json() if resp.content else {}


def test_patch_account_rename_only(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    a = _seed_account(session, seeded_user.id, name="Old CC", last4="1234")

    status_code, body = _patch(client, a.id, {"name": "New CC"})

    assert status_code == 200
    assert body["name"] == "New CC"
    assert body["last4"] == "1234"
    assert body["issuer"] == "axis"


def test_patch_account_null_name_rejected(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """An explicit ``null`` name is a 422, not a 500.

    ``name`` is NOT NULL on the column, so passing null used to reach
    ``setattr(..., None)`` and die at commit on an IntegrityError the 409 matchers
    don't recognise — surfacing as a catch-all 500. Matches the message
    /categories and /labels already return.
    """
    a = _seed_account(session, seeded_user.id, name="Keep me", last4="1234")

    status_code, body = _patch(client, a.id, {"name": None})

    assert status_code == 422
    assert "name cannot be cleared" in str(body["detail"])
    session.refresh(a)
    assert a.name == "Keep me"


def test_patch_account_lowercases_issuer(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    a = _seed_account(session, seeded_user.id, name="X", last4="1234", issuer="axis")

    status_code, body = _patch(client, a.id, {"issuer": "ICICI"})

    assert status_code == 200
    assert body["issuer"] == "icici"


def test_patch_cc_unsupported_issuer_rejected(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    a = _seed_account(session, seeded_user.id, name="X", last4="1234", issuer="axis")

    status_code, body = _patch(client, a.id, {"issuer": "hdfc"})

    assert status_code == 422
    assert "issuer must be one of" in str(body["detail"])


def test_patch_cc_null_issuer_rejected(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """Explicit null clears the parser key on a credit_card — rejected."""
    a = _seed_account(session, seeded_user.id, name="X", last4="1234", issuer="axis")

    status_code, body = _patch(client, a.id, {"issuer": None})

    assert status_code == 422
    assert "issuer must be one of" in str(body["detail"])


def test_patch_cc_reset_same_bad_issuer_still_rejected(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """A legacy CC with an unsupported stored issuer (seeded pre-guard) can't
    slip a no-op re-set past the idempotency short-circuit — the guard runs
    before it."""
    a = _seed_account(session, seeded_user.id, name="X", last4="1234", issuer="hdfc")

    status_code, body = _patch(client, a.id, {"issuer": "hdfc"})

    assert status_code == 422
    assert "issuer must be one of" in str(body["detail"])


def test_patch_account_empty_body_returns_unchanged_payload(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    a = _seed_account(session, seeded_user.id, name="X", last4="1234")

    status_code, body = _patch(client, a.id, {})

    assert status_code == 200
    assert body["name"] == "X"
    assert body["last4"] == "1234"


def test_patch_account_same_value_short_circuits(
    client: TestClient,
    seeded_user: User,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """Re-PATCHing every field with its current value should not bump updated_at."""
    a = _seed_account(session, seeded_user.id, name="X", last4="1234")
    with session_factory() as s:
        before = s.scalar(select(Account.updated_at).where(Account.id == a.id))

    status_code, _ = _patch(client, a.id, {"name": "X", "last4": "1234"})
    assert status_code == 200

    with session_factory() as s:
        after = s.scalar(select(Account.updated_at).where(Account.id == a.id))
    assert before == after


def test_patch_account_unknown_id_returns_404(
    client: TestClient,
    seeded_user: User,
) -> None:
    status_code, body = _patch(client, 99999, {"name": "X"})
    assert status_code == 404
    assert body["detail"] == "account not found"


def test_patch_account_foreign_user_returns_404(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    from uuid import uuid4

    other_user = User(id=uuid4())
    session.add(other_user)
    session.flush()
    foreign = Account(
        user_id=other_user.id,
        name="NotMine",
        type="credit_card",
        issuer="axis",
        last4="2222",
    )
    session.add(foreign)
    session.commit()

    status_code, body = _patch(client, foreign.id, {"name": "Mine"})
    assert status_code == 404
    # Confirm the foreign row was not mutated by the failed lookup.
    session.refresh(foreign)
    assert foreign.name == "NotMine"


def test_patch_account_archived_returns_404(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    a = _seed_account(
        session,
        seeded_user.id,
        name="Old",
        last4="1234",
        archived_at=datetime(2026, 1, 1, 12, 0, 0),
    )

    status_code, _ = _patch(client, a.id, {"name": "New"})
    assert status_code == 404


def test_patch_account_duplicate_name_returns_409(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    a = _seed_account(session, seeded_user.id, name="A", last4="1111")
    _seed_account(session, seeded_user.id, name="B", last4="2222")

    status_code, body = _patch(client, a.id, {"name": "B"})
    assert status_code == 409
    assert body["detail"] == "account name already exists"


def test_patch_account_parent_account_id_set_valid(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """CC → bank link via PATCH succeeds when the parent is a same-user active bank."""
    bank = _seed_account(session, seeded_user.id, name="HDFC Bank", type="bank", issuer="hdfc")
    cc = _seed_account(session, seeded_user.id, name="Axis CC", last4="1234")

    status_code, body = _patch(client, cc.id, {"parent_account_id": bank.id})

    assert status_code == 200
    assert body["parent_account_id"] == bank.id


def test_patch_account_parent_account_id_null_unlinks(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    bank = _seed_account(session, seeded_user.id, name="HDFC", type="bank", issuer="hdfc")
    cc = _seed_account(session, seeded_user.id, name="CC", last4="1234")
    # Pre-link directly so we can verify PATCH null unlinks it.
    cc.parent_account_id = bank.id
    session.commit()

    status_code, body = _patch(client, cc.id, {"parent_account_id": None})
    assert status_code == 200
    assert body["parent_account_id"] is None


def test_patch_account_self_parent_rejected(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    cc = _seed_account(session, seeded_user.id, name="CC", last4="1234")

    status_code, body = _patch(client, cc.id, {"parent_account_id": cc.id})
    assert status_code == 422
    assert "self" in body["detail"]


def test_patch_account_foreign_user_parent_rejected(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    from uuid import uuid4

    other_user = User(id=uuid4())
    session.add(other_user)
    session.flush()
    foreign_bank = Account(
        user_id=other_user.id,
        name="FBank",
        type="bank",
        issuer="hdfc",
    )
    session.add(foreign_bank)
    session.commit()
    cc = _seed_account(session, seeded_user.id, name="CC", last4="1234")

    status_code, body = _patch(client, cc.id, {"parent_account_id": foreign_bank.id})
    assert status_code == 422
    assert body["detail"] == "parent account not found or archived"


def test_patch_account_archived_parent_rejected(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    archived_bank = _seed_account(
        session,
        seeded_user.id,
        name="OldBank",
        type="bank",
        issuer="hdfc",
        archived_at=datetime(2026, 1, 1, 12, 0, 0),
    )
    cc = _seed_account(session, seeded_user.id, name="CC", last4="1234")

    status_code, body = _patch(client, cc.id, {"parent_account_id": archived_bank.id})
    assert status_code == 422
    assert body["detail"] == "parent account not found or archived"


def test_patch_account_non_bank_parent_rejected(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """A cash account isn't a valid parent — only ``type='bank'`` per PRD §F4a-1."""
    cash = _seed_account(
        session, seeded_user.id, name="Wallet", type="cash", issuer=None, last4=None
    )
    cc = _seed_account(session, seeded_user.id, name="CC", last4="1234")

    status_code, body = _patch(client, cc.id, {"parent_account_id": cash.id})
    assert status_code == 422
    assert body["detail"] == "parent account must be a bank account"


def test_patch_account_non_cc_self_with_parent_rejected(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """A bank account trying to set a parent → 422 (only CCs can have parents)."""
    bank_a = _seed_account(session, seeded_user.id, name="HDFC", type="bank", issuer="hdfc")
    bank_b = _seed_account(session, seeded_user.id, name="ICICI", type="bank", issuer="icici")

    status_code, body = _patch(client, bank_b.id, {"parent_account_id": bank_a.id})
    assert status_code == 422
    assert body["detail"] == "only credit_card accounts can have a parent"


def test_patch_account_rejects_locked_fields(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """type / currency / opening_balance_paise / archived_at are rejected by extra='forbid'."""
    a = _seed_account(session, seeded_user.id, name="X", last4="1234")

    for locked in (
        {"type": "bank"},
        {"currency": "USD"},
        {"opening_balance_paise": 1_000},
        {"archived_at": "2026-05-28T00:00:00"},
    ):
        status_code, _ = _patch(client, a.id, locked)
        assert status_code == 422, f"expected 422 for {locked}, got {status_code}"


def test_patch_account_rejects_extra_fields(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    a = _seed_account(session, seeded_user.id, name="X", last4="1234")

    status_code, _ = _patch(client, a.id, {"nickname": "fav-card"})
    assert status_code == 422


# ---------- DELETE ---------------------------------------------------------


def test_delete_account_archives(
    client: TestClient,
    seeded_user: User,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    a = _seed_account(session, seeded_user.id, name="Old", last4="1234")

    resp = client.delete(f"/api/v1/accounts/{a.id}")
    assert resp.status_code == 204
    assert resp.content == b""

    # Soft-delete: row remains, archived_at is set.
    with session_factory() as s:
        row = s.scalar(select(Account).where(Account.id == a.id))
        assert row is not None
        assert row.archived_at is not None
        # And it's filtered out of the active list.
        listed = client.get("/api/v1/accounts").json()
        assert all(r["id"] != a.id for r in listed)


def test_delete_account_unknown_returns_404(
    client: TestClient,
    seeded_user: User,
) -> None:
    resp = client.delete("/api/v1/accounts/99999")
    assert resp.status_code == 404


def test_delete_account_foreign_user_returns_404(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    from uuid import uuid4

    other_user = User(id=uuid4())
    session.add(other_user)
    session.flush()
    foreign = Account(
        user_id=other_user.id,
        name="NotMine",
        type="credit_card",
        issuer="axis",
        last4="2222",
    )
    session.add(foreign)
    session.commit()

    resp = client.delete(f"/api/v1/accounts/{foreign.id}")
    assert resp.status_code == 404
    # Foreign row must remain active — no silent archive.
    session.refresh(foreign)
    assert foreign.archived_at is None


def test_delete_account_already_archived_returns_404(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """Re-DELETE is idempotent: the loader filters archived rows → 404."""
    a = _seed_account(session, seeded_user.id, name="Old", last4="1234")

    first = client.delete(f"/api/v1/accounts/{a.id}")
    assert first.status_code == 204
    second = client.delete(f"/api/v1/accounts/{a.id}")
    assert second.status_code == 404


def test_delete_account_then_recreate_same_name_succeeds(
    client: TestClient,
    seeded_user: User,
    session: Session,
) -> None:
    """Partial-unique-index allows recreating an active account with an archived name."""
    a = _seed_account(session, seeded_user.id, name="Axis CC", last4="1234")
    resp = client.delete(f"/api/v1/accounts/{a.id}")
    assert resp.status_code == 204

    status_code, body = _post(
        client, {"name": "Axis CC", "type": "credit_card", "issuer": "axis", "last4": "5678"}
    )
    assert status_code == 201
    assert body["name"] == "Axis CC"
    assert body["id"] != a.id
