# What to Fake

Fake at **system boundaries** only:

- **External HTTP** (price feeds, FX rates, Google Drive) — through `httpx.MockTransport`, the pattern in
  `tests/services/test_fx_service.py`. No network in tests.
- **Time** — see below.
- **The database is not a boundary to fake.** Every DB test gets its own private, pre-built in-memory
  SQLite engine from the `clone_schema` fixture (`tests/conftest.py`). Use it; never mock a `Session`.

Don't fake:

- Your own services, parsers or helpers
- Internal collaborators
- Anything you control

## Time

`app/core/clock.py` is the only source of "now" (AGENTS.md §Timestamps). Prefer building data relative to
it over patching it:

```python
# GOOD: stamp the data in the past; the code reads the real clock
origin = clock.naive_utcnow() - timedelta(hours=ttl + 1)
```

Patch only when the code under test must see one fixed instant, and patch the `clock` module, never
`datetime`:

```python
monkeypatch.setattr(clock, "naive_utcnow", lambda: PINNED)
```

`tests/services/test_datetime_boundary.py` shows both, and why fast-forwarding the clock can re-implement
the logic under test.

## Designing for fakeability

At the boundaries, keep interfaces easy to fake:

**1. Pass the client in** rather than building it inside:

```python
# Easy to fake: the test passes an httpx.Client with a MockTransport
def fetch_rate(client: httpx.Client, on: date) -> int: ...

# Hard to fake: the network client is built internally
def fetch_rate(on: date) -> int:
    with httpx.Client() as client: ...
```

**2. One function per external operation** rather than a generic fetcher with branching, so each fake returns
one known shape and the test shows which endpoints it exercises.

Validate the external response at that boundary (AGENTS.md §Simplicity first) — that validation is what
the fake should exercise with bad payloads.
