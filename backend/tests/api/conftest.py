"""API-layer test fixtures.

Builds a fresh in-memory SQLite database per test (via :func:`make_engine`
so the FK-enforcement event listener fires exactly like production),
overrides :func:`app.core.db.get_db` to yield sessions from the test
engine, and pre-seeds the v1 User row.

Tests opt in by depending on ``client`` (the :class:`TestClient` with the
DB override wired up and the lifespan running against the test engine).
Tests that don't touch the DB (e.g. ``test_health.py``) skip these
fixtures and use ``TestClient(app)`` directly; structlog config still
fires via the session-scoped autouse fixture in :mod:`tests.conftest`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta
from decimal import Decimal
from functools import cache

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import clock, rate_limit
from app.core.config import get_settings
from app.core.db import get_db, make_engine
from app.core.demo import DEMO_EMAIL, DEMO_PASSWORD
from app.core.security import ACCESS_COOKIE_NAME, create_access_token, hash_password
from app.main import app
from app.models import Account, Category, Instrument, User
from app.services.nav_snapshot_service import as_valuation_stamp

# Any allowed CORS origin — set as a default header on test clients so the
# fail-closed CSRF Origin check (OriginCSRFMiddleware) lets mutations through.
_TEST_ORIGIN = "http://localhost:3000"


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> Iterator[None]:
    """Clear the in-process auth rate-limiter between tests so per-test attempt
    counts never bleed across tests (all share the TestClient IP)."""
    rate_limit.reset()
    yield
    rate_limit.reset()


@pytest.fixture
def engine(clone_schema: Callable[[Engine], None]) -> Iterator[Engine]:
    # StaticPool: single connection shared across threads. Required because
    # FastAPI runs sync endpoints in a thread pool, and the default
    # SingletonThreadPool would give each thread its own :memory: database.
    eng = make_engine("sqlite:///:memory:", poolclass=StaticPool)
    # Page-copied from the session template rather than built with create_all —
    # see ``clone_schema`` in tests/conftest.py. Still a private database.
    clone_schema(eng)
    try:
        yield eng
    finally:
        # No drop_all: with StaticPool the single pooled connection *is* the
        # ``:memory:`` database, so dispose() destroys the schema and the rows
        # together. Dropping 16 tables first cost ~5.6ms per test for nothing.
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = factory()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Per-request session factory used by the overridden ``get_db``.

    Distinct from the ``session`` fixture so assertions in the test body
    have their own session that sees committed-by-the-route writes
    without contention.
    """
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@cache
def _demo_password_hash() -> str:
    """The demo user's argon2id hash, computed once per session.

    argon2id costs ~105ms a call at production parameters, and effectively every test
    in this directory reaches ``seeded_user`` through ``client`` — hashing per test was
    ~82s of a full-suite run, a third of it. A salt only has to be unique per *user*,
    not per *test*, so one hash reused across the session is indistinguishable to every
    consumer: ``verify_password`` reads the parameters back out of the hash string, so
    the login paths still exercise a real verify.
    """
    return hash_password(DEMO_PASSWORD)


@pytest.fixture
def seeded_user(session: Session) -> User:
    """Seed the primary User row (id == v1_user_id) resolved by ``CurrentUserId``.

    Credentialed with the demo email/password so it doubles as the demo account
    for auth tests (login / "Try the demo") and honors the both-or-neither
    email/password_hash invariant (app-enforced; no DB CHECK).

    NOTE this row cannot log in under default settings — ``demo_login_permitted`` is
    off (ADR-0003 §Demo account gate). That's invisible to almost every test because
    ``client`` mints the access cookie directly rather than logging in; a test that
    POSTs /auth/login as this user needs the opt-in, or it gets a 401 whose cause is
    several files away.
    """
    user = User(
        id=get_settings().v1_user_id,
        email=DEMO_EMAIL,
        password_hash=_demo_password_hash(),
    )
    session.add(user)
    session.commit()
    return user


@pytest.fixture
def seeded_categories(session: Session, seeded_user: User) -> list[Category]:
    """The set of categories seeded by migration 0003."""
    spend_names = (
        "Food",
        "Groceries",
        "Transport",
        "Rent",
        "Utilities",
        "Shopping",
        "Entertainment",
        "Health",
        "Travel",
        "Subscriptions",
        "EMI",
        "Investment",
        "Other",
        "Transfer",
    )
    cats = [
        Category(user_id=seeded_user.id, name=n, kind="spend", is_seeded=True) for n in spend_names
    ]
    cats.append(Category(user_id=seeded_user.id, name="Income", kind="income", is_seeded=True))
    session.add_all(cats)
    session.commit()
    for c in cats:
        session.refresh(c)
    return cats


@pytest.fixture
def axis_account(session: Session, seeded_user: User) -> Account:
    """A single Axis credit-card account owned by the v1 user."""
    account = Account(
        user_id=seeded_user.id,
        name="Axis CC",
        type="credit_card",
        issuer="axis",
        last4="1234",
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


@pytest.fixture
def bank_account(session: Session, seeded_user: User) -> Account:
    """An HDFC bank account owned by the v1 user — the source/dest counterpart
    to ``axis_account`` for transfer tests."""
    account = Account(
        user_id=seeded_user.id,
        name="HDFC Bank",
        type="bank",
        issuer="hdfc",
        last4=None,
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


@pytest.fixture
def instrument(session: Session, seeded_user: User) -> Instrument:
    """A single INR mutual-fund instrument owned by the v1 user, with a NAV.

    The NAV is dated five days ago, not left NULL: ``nav_updated_at`` is the valuation
    date on every write path (:class:`app.models.instrument.Instrument`), so a priced
    instrument with no date is a state no writer produces. Five days is also past
    ``STALENESS_WARN_DAYS``, which keeps the fixture useful for the staleness surfaces.
    Relative to ``clock.today()`` so the resulting age is exactly 5 on any run.
    """
    inst = Instrument(
        user_id=seeded_user.id,
        symbol="INF209K01YV4",
        name="Index Fund Direct Growth",
        asset_class="indian_mf",
        currency="INR",
        exchange="MFCentral",
        current_nav=Decimal("150"),
        nav_updated_at=as_valuation_stamp(clock.today() - timedelta(days=5)),
    )
    session.add(inst)
    session.commit()
    session.refresh(inst)
    return inst


@pytest.fixture
def unauth_client(
    session_factory: sessionmaker[Session],
    seeded_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """TestClient with ``get_db`` overridden and lifespan-safe ``SessionLocal``,
    but NO auth cookie — for exercising register/login/refresh from scratch.

    Constructed with ``with`` so the FastAPI lifespan runs (needed for
    :func:`configure_logging`). ``monkeypatch`` rebinds ``app.main.SessionLocal``
    to the in-memory engine so the V1_USER_ID guard hits the test DB; the
    ``seeded_user`` dependency ensures that row exists. A default ``Origin``
    header satisfies the fail-closed CSRF check on mutating requests.
    """

    def _override_get_db() -> Iterator[Session]:
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    # Patches the imported name, not the source. Lifespan resolves `SessionLocal`
    # via app.main's namespace, where `from app.core.db import` copied the binding.
    monkeypatch.setattr("app.main.SessionLocal", session_factory)
    try:
        with TestClient(app) as c:
            c.headers["origin"] = _TEST_ORIGIN
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def client(unauth_client: TestClient, seeded_user: User) -> TestClient:
    """Authenticated TestClient — carries a valid access-token cookie for the
    seeded user, so every existing API test flows through the real auth layer
    (``CurrentUserId`` decodes this cookie) without a per-test login round-trip.
    """
    unauth_client.cookies.set(ACCESS_COOKIE_NAME, create_access_token(seeded_user.id))
    return unauth_client
