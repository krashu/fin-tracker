# Good and Bad Tests

Examples use this repo's fixtures: `client` / `unauth_client` / `seeded_user` from `tests/api/conftest.py`,
`session` / `user` from `tests/services/conftest.py`. Amounts are always integer paise.

## Good tests

**Integration-style**: test through the real interface, not mocks of internal parts.

```python
# GOOD: observable behaviour through the API
# (_post_txn / _row are module-level helpers, like _post in tests/api/test_accounts.py)
def test_refund_reduces_category_spend(client: TestClient, axis_account: Account) -> None:
    _post_txn(client, axis_account, amount_paise=-50_000, category="Dining")
    _post_txn(client, axis_account, amount_paise=20_000, category="Dining")  # refund (ADR-0009)

    spend = client.get("/api/v1/dashboards/spend-by-category").json()

    assert _row(spend, "Dining")["amount_paise"] == -30_000
```

Characteristics:

- Tests behaviour callers care about, through the public API or service function
- Survives internal refactors
- Describes WHAT, not HOW
- One logical assertion per test

## Bad tests

**Implementation-detail tests**: coupled to internal structure.

```python
# BAD: asserts on an internal collaborator
def test_import_calls_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(import_service, "fingerprint", lambda *a: calls.append(a) or "x")
    import_service.stage_rows(session, rows)
    assert len(calls) == 3
```

Red flags:

- Monkeypatching your own modules
- Testing `_private` helpers directly
- Asserting on call counts or order
- Breaks on a refactor without a behaviour change
- Verifying through a side channel instead of the interface

```python
# BAD: bypasses the interface to verify
def test_create_account_saves_row(client: TestClient, session: Session) -> None:
    client.post("/api/v1/accounts", json={"name": "HDFC", "type": "bank"})
    assert session.scalar(select(Account).where(Account.name == "HDFC")) is not None

# GOOD: verifies through the interface
def test_created_account_is_listed(client: TestClient) -> None:
    client.post("/api/v1/accounts", json={"name": "HDFC", "type": "bank"})
    names = [a["name"] for a in client.get("/api/v1/accounts").json()]
    assert names == ["HDFC"]
```

**Tautological tests**: the expected value restates the implementation.

```python
# BAD: recomputes the expected value the way the code does
def test_total_spend() -> None:
    rows = [-10_000, -5_000]
    assert total_spend_paise(rows) == sum(rows)

# GOOD: an independent, known literal
def test_total_spend() -> None:
    assert total_spend_paise([-10_000, -5_000]) == -15_000
```

**Isolation tests** are the exception to "don't test status codes alone": the 404 *is* the behaviour
(ADR-0003 rule 2). A new owned table gets a row in `tests/api/test_auth_isolation.py`, asserting another
user's id returns 404, never 403 or 200.
