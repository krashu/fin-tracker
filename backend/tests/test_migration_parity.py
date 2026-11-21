"""Schema parity: ``alembic upgrade head`` must match ``Base.metadata.create_all``.

Runs on every pytest invocation, the fast loop included; the tests below that
run upgrade/downgrade steps of their own are marked ``slow`` and run only in
the full suite. Catches the bug where a model
file is edited but the migration is not. Compares table set, column
name/type/**default**, unique constraints, foreign keys, indexes, primary
keys, and each CHECK constraint's **name + clause text** — plus an assertion
that CHECK names are unique per table.

This is the repo's ONLY structural defence against model/migration drift:
every other test builds its schema with ``create_all``, so app code is never
otherwise exercised against the migrated schema. It was previously blind in
three ways, all of which shipped a real defect:

* no default comparison — five columns carried a ``server_default`` in the
  migration and not in the model, so a raw ``INSERT`` succeeded on one DB and
  raised NOT NULL on the other;
* CHECKs counted, not compared (``.upper().count("CHECK")``) — blind to
  vocabulary drift (add ``"reit"`` to a Literal + its Enum but not the Alembic
  CHECK: the column stays ``VARCHAR(13)`` on both sides, the count is
  unchanged, CI is green, and the user's migrated DB raises ``CHECK constraint
  failed``) AND blind to duplicate names, since two constraints sharing one
  name still count as two;
* the mitigation the old docstring named — autogenerate's
  ``compare_server_default=True`` — is wired in :mod:`alembic.env` but never
  runs: ``alembic revision --autogenerate`` appears nowhere in the Makefile,
  CI, scripts or docs (``make migrate`` is ``alembic upgrade head``).

SQLite CHECK introspection is reliable enough for this: SQLAlchemy 2.0's
``get_check_constraints`` returns both name and ``sqltext`` on SQLite, which
the three deliberate-break tests below prove by going red.

A second test asserts the v1 ``users`` row is seeded by the migration with
``created_at`` populated (locks in the ``op.bulk_insert`` + ``server_default``
behavior so a Postgres swap doesn't silently drop the default).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from functools import cache
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, create_engine, event, inspect, select, text
from sqlalchemy.dialects import postgresql as postgresql_dialect
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateIndex

from alembic import command
from app.core.config import get_settings
from app.core.db import make_engine
from app.models import (
    Base,
    Benchmark,
    Category,
    Instrument,
    MerchantAlias,
    MerchantTagMap,
    Transaction,
    User,
)
from app.services.provisioning import (
    _DEFAULT_INCOME_TAXONOMY,
    _DEFAULT_SPEND_TAXONOMY,
    _MERCHANT_DICTIONARY,
    provision_default_categories,
)
from app.services.tag_service import pin_tag

BACKEND_ROOT = Path(__file__).parent.parent


def _alembic_cfg(connection: object) -> Config:
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    cfg.attributes["connection"] = connection
    return cfg


def _migration_engine(
    url: str = "sqlite:///:memory:", *, poolclass: type[object] | None = StaticPool
) -> Engine:
    """A throwaway SQLite engine for driving ``command.upgrade`` / ``downgrade``
    through this file's injected-connection path, with SQLite FK enforcement OFF
    from DBAPI connect time — the opposite of ``make_engine``.

    Migration 0032 seeds real ``merchant_tag_map`` rows against the default
    categories for every user, so by 0033 — whose ``add_column`` of a
    ``ForeignKey``'d ``parent_id`` Alembic can only apply on SQLite via a full
    DROP-and-recreate of ``categories`` (any FK'd column addition forces
    ``requires_recreate_in_batch`` to return True, self-referential or not) — a
    freshly-created in-memory test DB now always has a live row referencing
    ``categories`` before that recreate runs. Under real FK enforcement the
    implicit row-delete inside ``DROP TABLE`` trips it (see ``env.py``'s module
    docstring). ``PRAGMA foreign_keys`` can only be set reliably at connect time
    (it no-ops once a transaction is open — see the same docstring), so toggling
    it mid-run is not an option; building the engine FK-off from the start is.

    None of this file's assertions exercise runtime FK/CASCADE enforcement — they
    read schema shape, DDL text, and row survival — so this is safe for every
    test that only needs to get *through* the migration chain. A test that needs
    genuine FK enforcement (e.g. proving a downgrade leaves no dangling FK) must
    use its own file-backed engine pair instead, matching
    ``test_cli_upgrade_with_referencing_data_succeeds``.

    ``url``/``poolclass`` are overridable so ``test_migrations_stairway`` can point
    this at a real temp file (``poolclass=None``) instead of ``:memory:`` — a
    stairway walk opens many independent connections in sequence and a file
    survives that the same way a populated dev DB would, which is the case this
    helper exists to guard.
    """
    kwargs: dict[str, object] = {"connect_args": {"check_same_thread": False}}
    if poolclass is not None:
        kwargs["poolclass"] = poolclass
    engine = create_engine(url, **kwargs)

    @event.listens_for(engine, "connect")
    def _disable_fk(dbapi_connection, _record) -> None:  # noqa: ANN001 — alembic/SA event sig
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.close()

    return engine


def _sqlite_conn(raw: object) -> sqlite3.Connection:
    """The real ``sqlite3.Connection`` behind a pool checkout.

    SQLAlchemy types ``driver_connection`` as ``Any | None`` since it is
    dialect-dependent; every engine in this file is SQLite.
    """
    conn = getattr(raw, "driver_connection", None)
    assert isinstance(conn, sqlite3.Connection)
    return conn


@cache
def _head_template_engine() -> Engine:
    """One in-memory DB migrated to head, built once per process as a copy source.

    ``command.upgrade(..., "head")`` costs ~1.2s — 35 revisions of DDL plus data steps —
    and most tests below were each paying it to reach a byte-identical state. Cached, so
    the chain runs once; ``_head_engine`` then page-copies it in ~1ms.

    Held open for the life of the process on purpose: with ``StaticPool`` this engine's
    single DBAPI connection *is* the template database, so disposing it would destroy
    what every later clone reads from. Process exit reclaims it.

    Not used by the tests that upgrade to an *intermediate* revision first — the
    pre-migration shape is their subject — nor by ``test_migration_matches_models``,
    which is deliberately left running a real chain from scratch so that something
    independent of this template still proves a from-base upgrade matches the ORM.
    """
    eng = _migration_engine()
    with eng.begin() as conn:
        command.upgrade(_alembic_cfg(conn), "head")
    return eng


def _head_engine() -> Engine:
    """A private in-memory engine already at head. Caller disposes, as with
    ``_migration_engine``.

    The copy carries ``alembic_version`` at head along with the schema and seed rows,
    so ``command.downgrade`` runs against it exactly as it would against a DB this test
    had migrated itself. (``tests/conftest.py::clone_schema`` does the same trick for
    the ORM-built schema used by the api/services suites.)
    """
    eng = _migration_engine()
    target = eng.raw_connection()
    _sqlite_conn(_head_template_engine().raw_connection()).backup(_sqlite_conn(target))
    target.close()  # returns to the pool; StaticPool keeps it open
    return eng


@pytest.fixture()
def alembic_config() -> Config:
    """A bare ``Config`` (``script_location`` only) for ``test_migrations_stairway``.

    Modeled on ``common.alembic.testing.make_alembic_config``, a shared Alembic
    test helper used elsewhere at this org, which builds its ``Config`` straight
    from a session-scoped Postgres-testcontainer URL and hands the whole
    ``command.upgrade``/``downgrade`` sequence that single ``Config``. This
    project's ``env.py`` instead resolves its URL from ``get_settings()`` and
    branches on whether an existing connection was injected (see its module
    docstring) — there is no single ``Config`` that can drive the walk below, so
    this fixture supplies only the ``ScriptDirectory``-facing half (revision
    walking) and the test itself opens one connection per step, matching every
    other test in this file. Function-scoped rather than session-scoped: unlike a
    Postgres testcontainer, a throwaway SQLite file has no startup cost worth
    amortizing.
    """
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    return cfg


@pytest.mark.slow
def test_migrations_stairway(alembic_config: Config, tmp_path: Path) -> None:
    """Walk every migration base -> head: upgrade to it, downgrade to its parent,
    upgrade to it again. Proves every ``downgrade()`` in the chain actually runs
    and is reversible, one revision at a time — modeled on
    ``common.alembic.testing.run_stairway_test``.

    Structural only, on an empty DB: it cannot see a downgrade orphan a
    transaction (the targeted ``test_0034_downgrade_*`` test below pins that with
    real data) — but it does repeatedly hammer 0033's batch table-rebuild of
    ``categories`` after 0032 has, by this point in the walk, already made it a
    referenced table, on an engine with real FK enforcement (off only at connect
    time — see ``_migration_engine``) rather than a swallowed mid-run toggle. That
    is exactly the path the removed ``env.py`` ``PRAGMA foreign_keys=OFF`` hunk
    used to paper over. A real temp file, never ``:memory:``: each step below
    opens its own connection, and only a file — not an in-process ``:memory:`` DB
    without a shared pool — survives that the way a populated dev DB would.
    """
    revisions = list(ScriptDirectory.from_config(alembic_config).walk_revisions("base", "heads"))
    revisions.reverse()  # base -> head

    engine = _migration_engine(f"sqlite:///{(tmp_path / 'stairway.db').as_posix()}", poolclass=None)
    try:
        for rev in revisions:
            with engine.begin() as conn:
                command.upgrade(_alembic_cfg(conn), rev.revision)
            with engine.begin() as conn:
                command.downgrade(_alembic_cfg(conn), str(rev.down_revision or "base"))
            with engine.begin() as conn:
                command.upgrade(_alembic_cfg(conn), rev.revision)
    finally:
        engine.dispose()


def _snapshot(engine: object) -> dict[str, dict]:
    """Introspect an engine into a comparable dict.

    FKs and UQs carry their names, not just their columns: ADR-0001 rule 3 requires every
    constraint name to match between ``create_all`` and ``alembic upgrade head``, and a
    migration that names one by hand (or leaves it to the engine default) is otherwise
    invisible here until the first Postgres ``drop_constraint`` misses it. FKs also carry
    their ``ON DELETE``, which ADR-0012 rule 1 pins for ``categories.parent_id``.

    Skips the ``alembic_version`` bookkeeping table — present only on the
    migration side, would always cause a spurious diff.
    """
    insp = inspect(engine)  # type: ignore[arg-type]
    out: dict[str, dict] = {}
    for t in sorted(insp.get_table_names()):
        if t == "alembic_version":
            continue
        out[t] = {
            # ``default`` is the DB-side DEFAULT clause as introspected (a SQL string, e.g.
            # ``"'INR'"`` or ``CURRENT_TIMESTAMP``), not the Python-side ORM default — which is
            # invisible to the DB and therefore not a parity concern. Included because five
            # columns drifted here: the migration declared a server_default the model did not.
            "cols": {
                c["name"]: (str(c["type"]), bool(c["nullable"]), c.get("default"))
                for c in insp.get_columns(t)
            },
            "uqs": sorted(
                (u["name"] or "", tuple(u["column_names"])) for u in insp.get_unique_constraints(t)
            ),
            "ixs": sorted(
                (i["name"], tuple(i["column_names"]), bool(i["unique"]))
                for i in insp.get_indexes(t)
            ),
            "fks": sorted(
                (
                    f["name"] or "",  # unnamed reflects as None, unsortable beside a str
                    tuple(f["constrained_columns"]),
                    f["referred_table"],
                    tuple(f["referred_columns"]),
                    # ADR-0012 rule 1: CASCADE vs NO ACTION is schema, so it must match too.
                    f.get("options", {}).get("ondelete") or "",
                )
                for f in insp.get_foreign_keys(t)
            ),
            "pks": tuple(insp.get_pk_constraint(t)["constrained_columns"]),
        }
    return out


def _check_constraints(engine: object) -> dict[str, list[tuple[str | None, str]]]:
    """Introspect each table's CHECK constraints as sorted ``(name, clause text)`` pairs.

    Whitespace in the clause is collapsed so a reformatted-but-equivalent constraint doesn't
    read as drift; everything else — the name, the column, the vocabulary — must match
    verbatim. Sorted because declaration order is not a portability guarantee.
    """
    insp = inspect(engine)  # type: ignore[arg-type]
    out: dict[str, list[tuple[str | None, str]]] = {}
    for t in sorted(insp.get_table_names()):
        if t == "alembic_version":
            continue
        out[t] = sorted(
            (c.get("name"), " ".join(str(c.get("sqltext")).split()))
            for c in insp.get_check_constraints(t)
        )
    return out


def test_migration_matches_models() -> None:
    eng_migrate = _migration_engine()
    eng_orm = make_engine("sqlite:///:memory:", poolclass=StaticPool)
    try:
        with eng_migrate.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
        Base.metadata.create_all(eng_orm)

        snap_m = _snapshot(eng_migrate)
        snap_o = _snapshot(eng_orm)

        assert set(snap_m) == set(snap_o), f"tables differ: {set(snap_m) ^ set(snap_o)}"
        for t in sorted(snap_m):
            assert snap_m[t] == snap_o[t], (
                f"table {t} differs:\n  migrate: {snap_m[t]}\n  orm:     {snap_o[t]}"
            )

        checks_m = _check_constraints(eng_migrate)
        checks_o = _check_constraints(eng_orm)
        for t in sorted(checks_m):
            assert checks_m[t] == checks_o.get(t), (
                f"table {t} CHECK constraints differ:\n"
                f"  migrate: {checks_m[t]}\n  orm:     {checks_o.get(t)}"
            )

        # A per-table duplicate CHECK name is valid SQLite and invalid Postgres (42710), so
        # SQLite alone cannot catch it — and a count-based comparison never could, since two
        # constraints sharing one name still count as two. fx_rates shipped exactly this.
        for side, checks in (("migrate", checks_m), ("orm", checks_o)):
            for t, constraints in checks.items():
                names = [n for n, _ in constraints]
                assert len(names) == len(set(names)), (
                    f"{side}: table {t} has duplicate CHECK constraint names: {names}"
                )
                # ADR-0001 rule 3: a CHECK carries just its suffix; the naming convention adds
                # the ``ck_<table>_`` prefix, so a hand-written full name renders it twice.
                doubled = [n for n in names if n and n.startswith(f"ck_{t}_ck_")]
                assert doubled == [], f"{side}: table {t} has double-prefixed CHECKs: {doubled}"
    finally:
        eng_migrate.dispose()
        eng_orm.dispose()


def test_v1_user_seeded() -> None:
    eng = _head_engine()
    try:
        session_factory = sessionmaker(bind=eng)
        with session_factory() as s:
            users = list(s.scalars(select(User)))
            assert len(users) == 1
            assert users[0].id == get_settings().v1_user_id
            assert users[0].created_at is not None  # server_default fired on bulk_insert
    finally:
        eng.dispose()


# 15 spend seeds from 0003 + 4 income seeds from 0008. "Other" exists in both
# scopes, so the distinct-name set has 18 entries (income "Other" dedupes).
_EXPECTED_SEED_NAMES = frozenset(
    {
        # 0003 spend defaults
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
        "Income",  # flat seed, archived by 0008
        "Transfer",  # flat seed, archived by 0008
        "Other",
        # 0008 income seeds
        "Salary",
        "Freelancing",
        "Cashback",
    }
)
_INCOME_SEED_NAMES = frozenset({"Salary", "Freelancing", "Cashback", "Other"})
# Archived by the chain, frozen history: the flat Income/Transfer seeds (0008), the income
# Cashback that cashback-to-spend retired (ADR-0013), and the card-bill spend category 0047
# retired (ADR-0002 rule 7b).
_ARCHIVED_SEEDS = frozenset(
    {
        ("Income", "spend"),
        ("Transfer", "spend"),
        ("Cashback", "income"),
        ("Credit Card Payments", "spend"),
        ("Bills & Utilities", "spend"),
        ("Food", "spend"),
        ("Quick Bites & Snacks", "spend"),
        ("Restaurants & Cafes", "spend"),
        ("Coffee & Tea", "spend"),
        ("Online Food Delivery", "spend"),
        ("Instant Grocery Delivery", "spend"),
        ("Rent", "spend"),
        ("Utilities", "spend"),
        ("Electricity", "spend"),
        ("Cooking Gas & LPG", "spend"),
        ("Water & Municipal Taxes", "spend"),
        ("Mobile & Broadband", "spend"),
        ("Cable & Satellite TV", "spend"),
        ("Household Help & Domestic Staff", "spend"),
        ("Home Improvements & Repairs", "spend"),
        ("Furniture & Appliances", "spend"),
        ("Transport", "spend"),
        ("Travel", "spend"),
        ("Fuel & Petrol", "spend"),
        ("Metro & Public Transit", "spend"),
        ("Ride-Hailing & Taxis", "spend"),
        ("Highway Tolls & Parking", "spend"),
        ("Vehicle Service & Repairs", "spend"),
        ("Shopping", "spend"),
        ("Clothing & Apparel", "spend"),
        ("Footwear & Accessories", "spend"),
        ("Personal Care & Grooming", "spend"),
        ("Electronics & Gadgets", "spend"),
        ("Health", "spend"),
        ("Health & Life Insurance", "spend"),
        ("Subscriptions", "spend"),
        ("Digital Subscriptions & Streaming", "spend"),
        ("Entertainment", "spend"),
        ("Gifts & Celebrations", "spend"),
        ("Family Support & Transfers", "spend"),
        ("Education & Tuition", "spend"),
        ("Charity & Donations", "spend"),
        ("Investment", "spend"),
        ("Precious Metals & Gold", "spend"),
        ("Fixed Deposits & Savings", "spend"),
        ("EMI", "spend"),
        ("Loan EMIs & Repayments", "spend"),
        ("Home & Personal Loans", "spend"),
        ("Shared Expenses & Splits", "spend"),
    }
)


def test_default_categories_seeded() -> None:
    """After ``alembic upgrade head`` the v1 user has the 2-level category taxonomy seeded."""
    eng = _head_engine()
    try:
        session_factory = sessionmaker(bind=eng)
        with session_factory() as s:
            cats = list(
                s.scalars(select(Category).where(Category.user_id == get_settings().v1_user_id))
            )
            active = [c for c in cats if c.archived_at is None]
            parents = [c for c in active if c.parent_id is None]
            children = [c for c in active if c.parent_id is not None]
            assert all(c.is_seeded is True for c in cats)
            assert all(c.created_at is not None for c in cats)

            # The expected shape is read from provisioning.py's taxonomy constants, the one
            # source of truth (ADR-0012 rule 14), never restated here as counts or names.
            taxonomy = [("spend", entry) for entry in _DEFAULT_SPEND_TAXONOMY] + [
                ("income", entry) for entry in _DEFAULT_INCOME_TAXONOMY
            ]
            expected_parents = {(name, kind) for kind, (name, _, _) in taxonomy} | {
                ("system:investment_transfer", "spend")
            }
            assert {(c.name, c.kind) for c in parents} == expected_parents

            # A per-parent child-NAME set, not just a count — a same-count swap between two
            # siblings (e.g. a typo trading "Coffee & Tea" for something else) would still
            # pass a count.
            parent_ids = {(c.name, c.kind): c.id for c in parents}
            for kind, (name, _, subcategories) in taxonomy:
                pid = parent_ids[(name, kind)]
                assert {c.name for c in children if c.parent_id == pid} == set(subcategories), name
            assert len(children) == sum(len(subs) for _, (_, _, subs) in taxonomy)

            archived = [c for c in cats if c.archived_at is not None]
            assert {(c.name, c.kind) for c in archived} == _ARCHIVED_SEEDS
    finally:
        eng.dispose()


def test_provisioning_matches_migration_seed() -> None:
    """The migration-seeded demo user's ACTIVE categories must equal what
    ``provision_default_categories`` gives a fresh registrant."""
    eng = _head_engine()
    try:
        session_factory = sessionmaker(bind=eng)
        with session_factory() as s:
            migrated_cats = list(
                s.scalars(
                    select(Category).where(
                        Category.user_id == get_settings().v1_user_id,
                        Category.archived_at.is_(None),
                    )
                )
            )
            fresh_user_id = uuid.uuid4()
            provision_default_categories(s, fresh_user_id)
            fresh_cats = list(
                s.scalars(
                    select(Category).where(
                        Category.user_id == fresh_user_id,
                        Category.archived_at.is_(None),
                    )
                )
            )

            migrated_set = {
                (c.name, c.kind, c.parent_id is not None, c.color) for c in migrated_cats
            }
            fresh_set = {(c.name, c.kind, c.parent_id is not None, c.color) for c in fresh_cats}
            assert migrated_set == fresh_set
    finally:
        eng.dispose()


def test_seed_categories_have_default_color() -> None:
    """Every root parent category carries a default hex color, and every seeded child is
    ``NULL`` (decision #5, ADR-0012 / PRD §F5: a subcategory inherits its parent's hue;
    a seeded subcategory carrying its own hex is drift)."""
    eng = _head_engine()
    try:
        session_factory = sessionmaker(bind=eng)
        with session_factory() as s:
            active = list(
                s.scalars(
                    select(Category).where(
                        Category.user_id == get_settings().v1_user_id,
                        Category.archived_at.is_(None),
                    )
                )
            )
            parents = [c for c in active if c.parent_id is None]
            children = [c for c in active if c.parent_id is not None]

            uncolored = [c.name for c in parents if c.color is None]
            assert not uncolored, f"seeded categories missing a default color: {uncolored}"
            assert all(re.fullmatch(r"#[0-9a-f]{6}", c.color or "") for c in parents)

            colored_children = [c.name for c in children if c.color is not None]
            assert not colored_children, (
                f"seeded subcategories must be color=NULL to inherit the parent's hue, "
                f"but these carry their own: {colored_children}"
            )
    finally:
        eng.dispose()


# The 7 curated index funds seeded by 0014 (PRD §F8 view 5), by AMFI scheme code.
_EXPECTED_BENCHMARK_CODES = frozenset(
    {"120716", "143341", "147622", "147625", "147620", "145552", "148381"}
)


def test_benchmarks_seeded() -> None:
    """After ``alembic upgrade head`` the 7-row benchmark catalog exists (global
    reference data, no user_id), with valid numeric scheme codes — a typo'd code
    in the migration fails here, not in prod."""
    eng = _head_engine()
    try:
        session_factory = sessionmaker(bind=eng)
        with session_factory() as s:
            benchmarks = list(s.scalars(select(Benchmark)))
            assert len(benchmarks) == 7
            assert {b.amfi_code for b in benchmarks} == _EXPECTED_BENCHMARK_CODES
            # mfapi/AMFI scheme codes are numeric — guards a fat-fingered seed.
            assert all(b.amfi_code.isdigit() for b in benchmarks)
            assert all(b.kind == "index_fund" for b in benchmarks)
            assert all(b.currency == "INR" for b in benchmarks)
            assert all(b.archived_at is None for b in benchmarks)
            # server_default=now() must have fired during bulk_insert.
            assert all(b.created_at is not None for b in benchmarks)
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0014_downgrade_drops_benchmark_tables() -> None:
    """``alembic downgrade 0013`` from head drops both benchmark tables.

    Locks 0014's ``downgrade()`` (drop benchmark_nav before benchmarks — reverse FK
    order). ``test_migration_matches_models`` runs upgrade-side only and cannot catch
    downgrade drift (house precedent: 0003/0005/0008 each ship a downgrade test)."""
    eng = _head_engine()
    try:
        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), "0013_add_instrument_identifiers")

        insp = inspect(eng)
        assert not insp.has_table("benchmark_nav")
        assert not insp.has_table("benchmarks")
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0015_downgrade_drops_fx_rates() -> None:
    """``alembic downgrade 0014`` from head drops the ``fx_rates`` table.

    Locks 0015's ``downgrade()`` (single ``drop_table``). ``test_migration_matches_models``
    runs upgrade-side only and cannot catch downgrade drift (house precedent: 0003/0005/0008/
    0014 each ship a downgrade test)."""
    eng = _head_engine()
    try:
        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), "0014_add_benchmarks")

        assert not inspect(eng).has_table("fx_rates")
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0016_downgrade_narrows_instrument_index() -> None:
    """``alembic downgrade 0015`` from head reverts the instruments active-symbol unique index
    to the 2-column ``(user_id, symbol)`` form.

    Locks 0016's ``downgrade()``. Runs on schema-only data (no dual-currency rows), so the
    2-col recreate doesn't trip — the migration docstring flags that a downgrade with
    dual-currency symbols present is irreversible (forward-only widening). House precedent:
    0005/0008/0014/0015 each ship a downgrade test."""
    eng = _head_engine()
    try:
        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), "0015_add_fx_rates")

        insp = inspect(eng)
        names = {i["name"] for i in insp.get_indexes("instruments")}
        assert "uq_instruments_active_user_symbol_currency" not in names
        idx = next(
            i
            for i in insp.get_indexes("instruments")
            if i["name"] == "uq_instruments_active_user_symbol"
        )
        assert tuple(idx["column_names"]) == ("user_id", "symbol")
    finally:
        eng.dispose()


def test_instruments_partial_index_where_clause_preserved() -> None:
    """``uq_instruments_active_user_symbol_currency`` keeps ``archived_at IS NULL`` on both
    dialects and on the migration side.

    Mechanical drift catch (mirrors ``test_partial_index_where_clause_preserved``, which only
    covers ``transactions``): ``test_migration_matches_models`` compares ``(name, columns,
    unique)`` only — not the partial WHERE. The predicate is load-bearing — it's what lets a
    soft-deleted ``(symbol, currency)`` be re-created; dropping it on either side would make the
    unique index reject a re-create after archive."""
    idx = next(
        i
        for i in Instrument.__table__.indexes
        if i.name == "uq_instruments_active_user_symbol_currency"
    )
    rendered_sqlite = str(CreateIndex(idx).compile(dialect=sqlite_dialect.dialect()))
    assert "archived_at IS NULL" in rendered_sqlite, (
        f"model-side sqlite WHERE missing: {rendered_sqlite}"
    )
    rendered_pg = str(CreateIndex(idx).compile(dialect=postgresql_dialect.dialect()))
    assert "archived_at IS NULL" in rendered_pg, f"model-side postgres WHERE missing: {rendered_pg}"

    eng = _head_engine()
    try:
        with eng.begin() as conn:
            row = conn.execute(
                text(
                    "SELECT sql FROM sqlite_master WHERE type='index' "
                    "AND name='uq_instruments_active_user_symbol_currency'"
                )
            ).first()
        assert row is not None, (
            "uq_instruments_active_user_symbol_currency missing in sqlite_master"
        )
        assert "archived_at IS NULL" in row[0], (
            f"migration-side partial-index WHERE missing: {row[0]}"
        )
    finally:
        eng.dispose()


def test_partial_index_where_clause_preserved() -> None:
    """``ix_transactions_user_confirmed_date`` keeps ``confirmed_at IS NOT NULL`` on both sides.

    Mechanical drift catch: ``test_migration_matches_models`` compares
    ``(name, columns, unique)`` only and does **not** verify the partial-index
    WHERE predicate. Without this test, a contributor could silently drop
    ``sqlite_where=`` from the model or migration and the regular parity
    pass would not catch it — the WHERE clause is load-bearing for the
    board's hot-path query (``WHERE confirmed_at IS NOT NULL`` makes the
    index a partial one, dropping it would make every board read full-scan).
    """
    idx = next(
        i for i in Transaction.__table__.indexes if i.name == "ix_transactions_user_confirmed_date"
    )
    # Check both dialects independently. Dropping either ``sqlite_where=`` or
    # ``postgresql_where=`` on the model side would silently disable the
    # partial-index optimisation on the affected DB; this catches that drift.
    rendered_sqlite = str(CreateIndex(idx).compile(dialect=sqlite_dialect.dialect()))
    assert "confirmed_at IS NOT NULL" in rendered_sqlite, (
        f"model-side sqlite WHERE missing: {rendered_sqlite}"
    )
    rendered_pg = str(CreateIndex(idx).compile(dialect=postgresql_dialect.dialect()))
    assert "confirmed_at IS NOT NULL" in rendered_pg, (
        f"model-side postgres WHERE missing: {rendered_pg}"
    )

    eng = _head_engine()
    try:
        with eng.begin() as conn:
            row = conn.execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='index' "
                    "AND name='ix_transactions_user_confirmed_date'"
                )
            ).first()
        assert row is not None, "ix_transactions_user_confirmed_date missing in sqlite_master"
        assert "confirmed_at IS NOT NULL" in row[0], (
            f"migration-side partial-index WHERE missing: {row[0]}"
        )
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0005_downgrade_removes_constraints() -> None:
    """``alembic downgrade 0004_*`` from head removes the ADR-0002 constraints.

    Locks the migration 0005 ``downgrade()`` body: drops the composite
    unique index ``uq_transactions_id_user``, drops the no-self-pair
    CHECK, and restores the original single-column FK
    ``fk_transactions_transfer_pair_id_transactions``. Without this
    test, a contributor could leave ``downgrade()`` stale (e.g. only
    dropping the FK) and rollback would silently leave stale
    constraints behind. ``test_migration_matches_models`` runs on the
    upgrade side only — it cannot catch downgrade drift.
    """
    eng = _head_engine()
    try:
        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), "0004_add_transaction_confirmed_at")

        insp = inspect(eng)
        idx_names = {i["name"] for i in insp.get_indexes("transactions")}
        assert "uq_transactions_id_user" not in idx_names, (
            "downgrade did not drop the composite unique index"
        )

        fks = insp.get_foreign_keys("transactions")
        fk_names = {f["name"] for f in fks}
        assert "fk_transactions_transfer_pair_same_user" not in fk_names, (
            "downgrade did not drop the composite FK"
        )
        assert "fk_transactions_transfer_pair_id_transactions" in fk_names, (
            "downgrade did not restore the original single-column FK"
        )

        # CHECK introspection on SQLite goes through sqlite_master DDL.
        with eng.connect() as conn:
            row = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE type='table' AND name='transactions'")
            ).first()
        assert row is not None
        assert "ck_transactions_no_self_pair" not in (row[0] or ""), (
            f"downgrade did not drop the no-self-pair CHECK: {row[0]}"
        )
    finally:
        eng.dispose()


@pytest.mark.slow
def test_default_categories_downgrade_removes_seeded_only() -> None:
    """0003 downgrade deletes seeded rows (incl. archived) and keeps user-made (incl. archived)."""
    eng = _head_engine()
    try:
        # Seed three more rows to lock down the archived-row edges:
        #   - UserMade (active, is_seeded=False)        → survives
        #   - ArchivedUserMade (archived, is_seeded=False) → survives
        #   - ArchivedSeeded (archived, is_seeded=True)  → deleted
        # The migration's WHERE has no archived_at clause; if someone
        # "optimises" it to skip archived rows, this test fails.
        now = datetime.now(UTC)
        session_factory = sessionmaker(bind=eng)
        with session_factory() as s:
            s.add_all(
                [
                    Category(
                        color="#2a78d6",
                        user_id=get_settings().v1_user_id,
                        name="UserMade",
                        is_seeded=False,
                    ),
                    Category(
                        color="#2a78d6",
                        user_id=get_settings().v1_user_id,
                        name="ArchivedUserMade",
                        is_seeded=False,
                        archived_at=now,
                    ),
                    Category(
                        color="#2a78d6",
                        user_id=get_settings().v1_user_id,
                        name="ArchivedSeeded",
                        is_seeded=True,
                        archived_at=now,
                    ),
                ]
            )
            s.commit()

        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), "0002_account_name_unique")

        # Smoke: schema for `categories` is untouched by the data-only downgrade.
        assert inspect(eng).has_table("categories")

        with session_factory() as s:
            # Column-level select (not the whole entity): post-downgrade the
            # categories table predates the kind column, so select(Category)
            # would emit a SELECT of the now-absent kind column and fail.
            remaining = s.execute(
                select(Category.name, Category.is_seeded).where(
                    Category.user_id == get_settings().v1_user_id
                )
            ).all()
            names = {r.name for r in remaining}
            assert names == {"UserMade", "ArchivedUserMade"}
            assert "ArchivedSeeded" not in names
            assert all(r.is_seeded is False for r in remaining)
    finally:
        eng.dispose()


@pytest.mark.slow
def test_cli_upgrade_with_referencing_data_succeeds(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """Regression: a *populated* DB must survive batch table-rebuild migrations.

    0008 (categories) and 0009 (transactions) rebuild a *referenced* table via
    ``batch_alter_table``. With ``foreign_keys=ON``, SQLite's implicit row-delete
    during the rebuild's ``DROP TABLE`` trips the child FK once real rows exist —
    the empty in-memory DBs every other test here uses never see it, so it
    shipped in 0008 and only bit a populated dev DB. ``env.py``'s CLI path runs
    migrations with FK OFF; this drives that path (no injected connection) against
    a temp-file DB carrying a ``merchant_tag_map`` row that references a category
    staged at 0007, then asserts the upgrade to head completes and the reference
    survives the rebuild (batch preserves ids).

    The category gets a colour at 0011, since 0046's CHECK refuses a colourless root.
    """
    import app.core.config as config_mod

    db_url = f"sqlite:///{(tmp_path / 'reg.db').as_posix()}"
    # Point env.py at the temp DB: it reads get_settings().database_url at exec
    # time, so patching the attribute is enough (only database_url overridden;
    # v1_user_id keeps its fixed default, which 0001 seeds).
    monkeypatch.setattr(
        config_mod, "get_settings", lambda: config_mod.Settings(database_url=db_url)
    )

    def _cli_cfg() -> Config:
        # No attributes["connection"] → env builds its own engine (the CLI path
        # under test, which installs the FK-OFF listener).
        cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
        return cfg

    command.upgrade(_cli_cfg(), "0007_add_investment_tables")

    # Seed a category + the cheapest child FK into it (a merchant_tag_map row).
    # Raw SQL: the current ORM Category carries a `kind` column absent at 0007.
    # uuid is stored as 32-char hex (SQLAlchemy Uuid on SQLite).
    uid_hex = uuid.UUID("00000000-0000-0000-0000-000000000001").hex
    seed = make_engine(db_url)
    try:
        with seed.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO categories (user_id, name, is_seeded) VALUES (:u, 'RegCat', 0)"
                ).bindparams(u=uid_hex)
            )
            cat_id = conn.execute(text("SELECT id FROM categories WHERE name = 'RegCat'")).scalar()
            conn.execute(
                text(
                    "INSERT INTO merchant_tag_map (user_id, merchant_normalized, category_id) "
                    "VALUES (:u, 'regmerchant', :c)"
                ).bindparams(u=uid_hex, c=cat_id)
            )
    finally:
        seed.dispose()

    # The line that failed pre-fix (IntegrityError at 0008's DROP TABLE categories).
    # The category is staged at 0007, before ``color`` exists (0011). Root categories need a
    # colour from 0046 on, so give it one once the column is there, then run the rest of the chain.
    command.upgrade(_cli_cfg(), "0011_category_color")
    colour = make_engine(db_url)
    try:
        with colour.begin() as conn:
            conn.execute(text("UPDATE categories SET color = '#2a78d6' WHERE name = 'RegCat'"))
    finally:
        colour.dispose()
    command.upgrade(_cli_cfg(), "head")

    check = make_engine(db_url)
    try:
        cols = {c["name"] for c in inspect(check).get_columns("transactions")}
        assert "auto_category_id" in cols  # 0009 applied
        with check.connect() as conn:
            # Category survived the rebuild with its id intact → the reference resolves.
            still = conn.execute(
                text(
                    "SELECT category_id FROM merchant_tag_map "
                    "WHERE merchant_normalized = 'regmerchant'"
                )
            ).scalar()
            assert still == cat_id
    finally:
        check.dispose()


@pytest.mark.slow
def test_cli_upgrade_with_investment_data_succeeds(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """Regression: 0010 rebuilds *populated* ``investment_transactions`` cleanly.

    0010 adds ``import_batch_id`` (a FK) + ``fingerprint`` to
    ``investment_transactions`` via ``batch_alter_table``, which DROP+recreates the
    table. That table carries the self-referential composite FK
    ``fk_investment_transactions_switch_pair_same_user`` (migration 0007); the rebuild
    re-emits it, and on a *populated* table the copy step validates only because the
    composite-unique target ``uq_investment_transactions_id_user`` already exists. The
    empty in-memory ``test_migration_matches_models`` DB never exercises the copy with
    real rows. This drives the CLI path (FK OFF) against a temp-file DB carrying a real
    instrument + investment_transaction staged at 0009, then asserts the upgrade to head
    completes, the row survives with its id intact, and an account-less (CAS) batch can
    be inserted post-0010 (the ``account_id`` nullable change took effect).
    """
    import app.core.config as config_mod

    db_url = f"sqlite:///{(tmp_path / 'inv.db').as_posix()}"
    monkeypatch.setattr(
        config_mod, "get_settings", lambda: config_mod.Settings(database_url=db_url)
    )

    def _cli_cfg() -> Config:
        cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
        return cfg

    command.upgrade(_cli_cfg(), "0009_add_transaction_auto_category_id")

    # Seed an instrument + one investment_transaction via raw SQL: the current ORM
    # models carry import_batch_id/fingerprint columns absent at 0009. Scaled-int
    # storage (units 1e8, fx 1e6); values are arbitrary — we only check survival.
    uid_hex = uuid.UUID("00000000-0000-0000-0000-000000000001").hex
    seed = make_engine(db_url)
    try:
        with seed.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO instruments "
                    "(user_id, symbol, name, asset_class, currency, exchange) "
                    "VALUES (:u, 'INF209K01YV4', 'Reg Fund', 'indian_mf', 'INR', 'MFCentral')"
                ).bindparams(u=uid_hex)
            )
            inst_id = conn.execute(
                text("SELECT id FROM instruments WHERE symbol = 'INF209K01YV4'")
            ).scalar()
            conn.execute(
                text(
                    "INSERT INTO investment_transactions "
                    "(user_id, instrument_id, date, transaction_type, units, "
                    " amount_native_paise, fees_native_paise, fx_rate_to_inr) "
                    "VALUES (:u, :i, '2025-01-15', 'buy', 10000000000, 100000, 0, 1000000)"
                ).bindparams(u=uid_hex, i=inst_id)
            )
            txn_id = conn.execute(
                text("SELECT id FROM investment_transactions WHERE instrument_id = :i").bindparams(
                    i=inst_id
                )
            ).scalar()
            # Step 1 of 0010 rebuilds import_batches — a *referenced* table. Seed a
            # spend transaction referencing an import_batches row so that rebuild is
            # exercised with a live external FK referrer on a populated DB (the harder
            # path; step 2's self-ref rebuild is covered by the investment_transaction
            # above).
            conn.execute(
                text(
                    "INSERT INTO accounts (user_id, name, type) "
                    "VALUES (:u, 'Axis CC', 'credit_card')"
                ).bindparams(u=uid_hex)
            )
            acct_id = conn.execute(text("SELECT id FROM accounts WHERE name = 'Axis CC'")).scalar()
            conn.execute(
                text(
                    "INSERT INTO import_batches "
                    "(user_id, account_id, source_file_hash, parser_name, status) "
                    "VALUES (:u, :a, 'spendhash', 'AxisCC', 'completed')"
                ).bindparams(u=uid_hex, a=acct_id)
            )
            batch_id = conn.execute(
                text("SELECT id FROM import_batches WHERE source_file_hash = 'spendhash'")
            ).scalar()
            conn.execute(
                text(
                    "INSERT INTO transactions "
                    "(user_id, account_id, date, amount_paise, transaction_type, "
                    " merchant_normalized, fingerprint, source, import_batch_id) "
                    "VALUES (:u, :a, '2025-01-10', -5000, 'spend', 'amazon', "
                    "        'fp-spend-1', 'import', :b)"
                ).bindparams(u=uid_hex, a=acct_id, b=batch_id)
            )
            # Capture the id NOW: 0025 recomputes every fingerprint, so the
            # 'fp-spend-1' literal is not a usable key after the upgrade.
            spend_txn_id = conn.execute(
                text("SELECT id FROM transactions WHERE merchant_normalized = 'amazon'")
            ).scalar()
    finally:
        seed.dispose()

    # The rebuild that this test exists to cover.
    command.upgrade(_cli_cfg(), "head")

    check = make_engine(db_url)
    try:
        cols = {c["name"] for c in inspect(check).get_columns("investment_transactions")}
        assert "import_batch_id" in cols and "fingerprint" in cols  # 0010 applied
        with check.connect() as conn:
            # The investment_transaction survived the rebuild with its id intact.
            survived = conn.execute(
                text("SELECT units FROM investment_transactions WHERE id = :t").bindparams(t=txn_id)
            ).scalar()
            assert survived == 10000000000
            # account_id is now nullable: an account-less investment batch inserts cleanly.
            conn.execute(
                text(
                    "INSERT INTO import_batches "
                    "(user_id, account_id, source_file_hash, parser_name, status) "
                    "VALUES (:u, NULL, 'deadbeef', 'investment_csv', 'completed')"
                ).bindparams(u=uid_hex)
            )
            inv_batches = conn.execute(
                text(
                    "SELECT COUNT(*) FROM import_batches "
                    "WHERE account_id IS NULL AND parser_name = 'investment_csv'"
                )
            ).scalar()
            assert inv_batches == 1
            # The import_batches rebuild (step 1) preserved the spend transaction's
            # FK to its batch — batch ids survive the rebuild (FK OFF in env.py).
            # Keyed by id, not by fingerprint: 0025's recompute rewrote the
            # 'fp-spend-1' literal by design.
            still_linked = conn.execute(
                text("SELECT import_batch_id FROM transactions WHERE id = :t").bindparams(
                    t=spend_txn_id
                )
            ).scalar()
            assert still_linked == batch_id
    finally:
        check.dispose()


@pytest.mark.slow
def test_0008_downgrade_removes_kind_and_income_seeds() -> None:
    """``alembic downgrade 0007`` from head reverses the category-kind migration.

    Locks 0008's ``downgrade()``: drop the ``kind`` column (and its CHECK),
    delete the 4 income seeds, un-archive the flat Income/Transfer seeds, and
    restore the 2-column active-name unique index. ``test_migration_matches_models``
    runs on the upgrade side only and cannot catch downgrade drift.
    """
    eng = _head_engine()
    try:
        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), "0007_add_investment_tables")

        insp = inspect(eng)
        col_names = {c["name"] for c in insp.get_columns("categories")}
        assert "kind" not in col_names, "downgrade did not drop the kind column"

        # Active-name unique index is back to 2 columns.
        idx = next(
            i
            for i in insp.get_indexes("categories")
            if i["name"] == "uq_categories_active_user_name"
        )
        assert tuple(idx["column_names"]) == ("user_id", "name")

        # The enum CHECK is gone from the table DDL.
        with eng.connect() as conn:
            ddl = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE type='table' AND name='categories'")
            ).scalar()
        assert "category_kind" not in (ddl or "")

        session_factory = sessionmaker(bind=eng)
        with session_factory() as s:
            # Column-level select: kind no longer exists post-downgrade, so
            # selecting the whole Category entity would fail.
            rows = s.execute(
                select(Category.name, Category.archived_at).where(
                    Category.user_id == get_settings().v1_user_id
                )
            ).all()
            names = {r.name for r in rows}
            # Income seeds deleted; the 15 spend seeds remain, all active again.
            assert names == (_EXPECTED_SEED_NAMES - _INCOME_SEED_NAMES) | {"Other"}
            assert {"Salary", "Freelancing", "Cashback"}.isdisjoint(names)
            assert len(rows) == 15
            assert all(r.archived_at is None for r in rows), "flat seeds not un-archived"
    finally:
        eng.dispose()


def _insert_spend_row(conn: object, *, uid_hex: str, acct_id: int, fingerprint: str) -> None:
    """Raw-SQL spend row that does NOT name ``occurrence`` — proving the
    server_default covers the parity suite's own inserts."""
    conn.execute(  # ty: ignore[unresolved-attribute]
        text(
            "INSERT INTO transactions "
            "(user_id, account_id, date, amount_paise, transaction_type, "
            " merchant_normalized, fingerprint, source) "
            "VALUES (:u, :a, '2025-01-10', -5000, 'spend', 'amazon', :f, 'import')"
        ).bindparams(u=uid_hex, a=acct_id, f=fingerprint)
    )


@pytest.mark.slow
def test_0026_downgrade_restores_switch_pair_id() -> None:
    """``alembic downgrade 0025`` from head renames ``pair_id`` back.

    Also pins the SQLite behaviour 0026 depends on: a native ``RENAME COLUMN``
    rewrites the FK clause and the CHECK clause to reference the new name. Without
    this, a future SQLite or Alembic change could silently leave a CHECK pointing at
    a column that no longer exists. House precedent: each migration ships a
    downgrade test.
    """
    eng = _head_engine()
    try:

        def _state() -> tuple[set[str], tuple[str, ...], str]:
            insp = inspect(eng)
            cols = {c["name"] for c in insp.get_columns("investment_transactions")}
            # The self-referential pair FK — not "the first composite FK": 0037 added two more,
            # and a later batch rebuild is free to reorder them.
            fk = next(
                tuple(f["constrained_columns"])
                for f in insp.get_foreign_keys("investment_transactions")
                if f["referred_table"] == "investment_transactions"
            )
            with eng.connect() as conn:
                ddl = conn.execute(
                    text(
                        "SELECT sql FROM sqlite_master WHERE type = 'table' "
                        "AND name = 'investment_transactions'"
                    )
                ).scalar()
            return cols, fk, ddl or ""

        cols, fk, ddl = _state()
        assert "pair_id" in cols and "switch_pair_id" not in cols
        assert fk == ("pair_id", "user_id")
        assert "pair_id IS NULL OR pair_id != id" in ddl

        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), "0025_fingerprint_separator_and_occurrence")
        cols, fk, ddl = _state()
        assert "switch_pair_id" in cols and "pair_id" not in cols
        assert fk == ("switch_pair_id", "user_id")
        assert "switch_pair_id IS NULL OR switch_pair_id != id" in ddl

        # Re-upgrade — up → down → up is clean.
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
        cols, fk, _ = _state()
        assert "pair_id" in cols and "switch_pair_id" not in cols
        assert fk == ("pair_id", "user_id")
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0025_recomputes_existing_fingerprints() -> None:
    """The only test that proves 0025's backfill actually ran.

    Seed a row at 0024 carrying the OLD separatorless hash, upgrade to head, and
    assert the stored value is now the ``\\x1f``-joined hash. Also asserts the
    ``occurrence`` server_default covers a raw INSERT that never names it.
    """
    eng = _migration_engine()
    try:
        uid_hex = uuid.uuid4().hex
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "0024_add_pinned_to_merchant_maps")
            conn.execute(text("INSERT INTO users (id) VALUES (:u)").bindparams(u=uid_hex))
            conn.execute(
                text(
                    "INSERT INTO accounts (user_id, name, type) "
                    "VALUES (:u, 'Axis CC', 'credit_card')"
                ).bindparams(u=uid_hex)
            )
            acct_id = conn.execute(text("SELECT id FROM accounts")).scalar()
            old_fp = hashlib.sha256(f"2025-01-10{-5000}amazon{acct_id}".encode()).hexdigest()
            _insert_spend_row(conn, uid_hex=uid_hex, acct_id=acct_id, fingerprint=old_fp)

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")

        new_fp = hashlib.sha256(
            "\x1f".join(("2025-01-10", "-5000", "amazon", str(acct_id))).encode()
        ).hexdigest()
        with eng.connect() as conn:
            stored, occurrence = conn.execute(
                text("SELECT fingerprint, occurrence FROM transactions")
            ).one()
        assert stored == new_fp
        assert stored != old_fp
        assert occurrence == 0  # server_default covered the raw INSERT
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0025_downgrade_restores_the_old_key_and_is_reversible() -> None:
    """``alembic downgrade 0024`` from head drops ``occurrence``, re-narrows the
    unique constraint to 3 columns, and restores the separatorless fingerprints;
    re-upgrading returns to the ``\\x1f`` form. House precedent: each migration
    ships a downgrade test; ``test_migration_matches_models`` runs upgrade-only."""
    eng = _head_engine()
    try:
        uid_hex = uuid.uuid4().hex
        with eng.begin() as conn:
            conn.execute(text("INSERT INTO users (id) VALUES (:u)").bindparams(u=uid_hex))
            conn.execute(
                text(
                    "INSERT INTO accounts (user_id, name, type) "
                    "VALUES (:u, 'Axis CC', 'credit_card')"
                ).bindparams(u=uid_hex)
            )
            acct_id = conn.execute(text("SELECT id FROM accounts")).scalar()
            _insert_spend_row(conn, uid_hex=uid_hex, acct_id=acct_id, fingerprint="whatever")

        def _uq_columns() -> list[str]:
            return next(
                uq["column_names"]
                for uq in inspect(eng).get_unique_constraints("transactions")
                if uq["name"] == "uq_transactions_user_account_fingerprint"
            )

        assert "occurrence" in _uq_columns()

        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), "0024_add_pinned_to_merchant_maps")

        insp = inspect(eng)
        assert "occurrence" not in {c["name"] for c in insp.get_columns("transactions")}
        assert _uq_columns() == ["user_id", "account_id", "fingerprint"]
        with eng.connect() as conn:
            assert conn.execute(text("SELECT fingerprint FROM transactions")).scalar() == (
                hashlib.sha256(f"2025-01-10{-5000}amazon{acct_id}".encode()).hexdigest()
            )

        # Re-upgrade — up → down → up is clean.
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
        assert "occurrence" in _uq_columns()
        new_payload = "\x1f".join(("2025-01-10", "-5000", "amazon", str(acct_id)))
        with eng.connect() as conn:
            assert conn.execute(text("SELECT fingerprint FROM transactions")).scalar() == (
                hashlib.sha256(new_payload.encode()).hexdigest()
            )
    finally:
        eng.dispose()


def test_0025_batch_rebuild_preserves_the_partial_index_predicate() -> None:
    """0025 rebuilds ``transactions``; the board's partial index must keep its
    WHERE clause. ``test_migration_matches_models`` does NOT compare partial
    predicates (see ``Transaction.__table_args__``), so assert it here rather than
    inherit that silence — mirrors ``test_partial_index_where_clause_preserved``."""
    eng = _head_engine()
    try:
        with eng.connect() as conn:
            sql = conn.execute(
                text(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' "
                    "AND name = 'ix_transactions_user_confirmed_date'"
                )
            ).scalar()
        assert sql is not None
        assert "confirmed_at IS NOT NULL" in sql
    finally:
        eng.dispose()


def _seed_investment_row(
    conn: object, *, uid_hex: str, fingerprint: str, units_scaled: int = 1_000_000_000
) -> tuple[int, int]:
    """A user + instrument + ONE investment row, staged for the 0027 tests.

    ``units`` is written as the RAW scaled int (8 dp) the ORM's ``Units`` decorator
    stores — 0027's recompute reads it back through a plain ``sa.Integer`` column and
    hashes it verbatim, so this is the value the payload must contain. The INSERT
    deliberately does NOT name ``occurrence``, proving the server_default covers the
    parity suite's own raw inserts (see ``_insert_spend_row``). Returns
    ``(instrument_id, txn_id)``."""
    conn.execute(  # ty: ignore[unresolved-attribute]
        text("INSERT INTO users (id) VALUES (:u)").bindparams(u=uid_hex)
    )
    conn.execute(  # ty: ignore[unresolved-attribute]
        text(
            "INSERT INTO instruments (user_id, symbol, name, asset_class, currency, exchange) "
            "VALUES (:u, 'INFY', 'Infosys', 'indian_equity', 'INR', 'NSE')"
        ).bindparams(u=uid_hex)
    )
    inst_id = conn.execute(  # ty: ignore[unresolved-attribute]
        text("SELECT id FROM instruments WHERE symbol = 'INFY'")
    ).scalar()
    cols = {
        row[1]
        for row in conn.execute(  # ty: ignore[unresolved-attribute]
            text("PRAGMA table_info(investment_transactions)")
        ).fetchall()
    }
    if "account_id" in cols:
        conn.execute(  # ty: ignore[unresolved-attribute]
            text(
                "INSERT INTO investment_accounts "
                "(user_id, name, account_type, provider, external_account_ref, currency) "
                "VALUES (:u, 'Default', 'demat', 'other', 'default', 'INR')"
            ).bindparams(u=uid_hex)
        )
        acct_id = conn.execute(  # ty: ignore[unresolved-attribute]
            text("SELECT id FROM investment_accounts WHERE user_id = :u").bindparams(u=uid_hex)
        ).scalar()
        conn.execute(  # ty: ignore[unresolved-attribute]
            text(
                "INSERT INTO investment_transactions "
                "(user_id, account_id, instrument_id, date, transaction_type, units, "
                " amount_native_paise, fees_native_paise, fx_rate_to_inr, fingerprint) "
                "VALUES (:u, :a, :i, '2025-01-15', 'buy', :n, 100000, 0, 1000000, :f)"
            ).bindparams(u=uid_hex, a=acct_id, i=inst_id, n=units_scaled, f=fingerprint)
        )
    else:
        conn.execute(  # ty: ignore[unresolved-attribute]
            text(
                "INSERT INTO investment_transactions "
                "(user_id, instrument_id, date, transaction_type, units, "
                " amount_native_paise, fees_native_paise, fx_rate_to_inr, fingerprint) "
                "VALUES (:u, :i, '2025-01-15', 'buy', :n, 100000, 0, 1000000, :f)"
            ).bindparams(u=uid_hex, i=inst_id, n=units_scaled, f=fingerprint)
        )
    txn_id = conn.execute(  # ty: ignore[unresolved-attribute]
        text("SELECT id FROM investment_transactions WHERE fingerprint = :f").bindparams(
            f=fingerprint
        )
    ).scalar()
    return inst_id, txn_id


def _investment_fp(separator: str, *, inst_id: int, units_scaled: int = 1_000_000_000) -> str:
    """0027's payload, spelled out independently of the migration (and of the service)
    so the test would catch a field-order or scale change, not just mirror it."""
    return hashlib.sha256(
        separator.join((str(inst_id), "2025-01-15", "buy", "100000", str(units_scaled))).encode()
    ).hexdigest()


def _investment_uq_columns(engine: object) -> list[str]:
    return next(
        ix["column_names"]
        for ix in inspect(engine).get_indexes("investment_transactions")  # ty: ignore[invalid-argument-type]
        if ix["name"] == "uq_investment_transactions_user_instrument_fingerprint"
    )


@pytest.mark.slow
def test_0027_recomputes_investment_fingerprints() -> None:
    """The only test that proves 0027's backfill actually ran.

    Seed a row at 0026 carrying the OLD separatorless hash, upgrade to head, and assert
    the stored value is now the ``\\x1f``-joined hash. Also pins the two things the
    recompute could silently get wrong: that ``units`` is hashed as the RAW scaled int
    (reading it through the ``Units`` TypeDecorator would unscale it and change every
    hash), and that the ``occurrence`` server_default covers a raw INSERT that never
    names it."""
    eng = _migration_engine()
    try:
        uid_hex = uuid.uuid4().hex
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "0026_rename_switch_pair_id_to_pair_id")
            # Placeholder: the real old hash needs inst_id, which the insert assigns.
            inst_id, txn_id = _seed_investment_row(conn, uid_hex=uid_hex, fingerprint="seed")
            old_fp = _investment_fp("", inst_id=inst_id)
            conn.execute(
                text(
                    "UPDATE investment_transactions SET fingerprint = :f WHERE id = :t"
                ).bindparams(f=old_fp, t=txn_id)
            )

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")

        with eng.connect() as conn:
            stored, occurrence = conn.execute(
                text("SELECT fingerprint, occurrence FROM investment_transactions")
            ).one()
        assert stored == _investment_fp("\x1f", inst_id=inst_id)
        assert stored != old_fp
        assert occurrence == 0  # server_default covered the raw INSERT
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0027_recompute_leaves_manual_null_fingerprints_alone() -> None:
    """A manual row carries ``fingerprint = NULL``, and NULLs-are-distinct is what keeps
    the unique index inert for manual entry. Hashing one would silently enrol it in
    dedup, so the recompute's ``IS NOT NULL`` filter is load-bearing."""
    eng = _migration_engine()
    try:
        uid_hex = uuid.uuid4().hex
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "0026_rename_switch_pair_id_to_pair_id")
            inst_id, _ = _seed_investment_row(conn, uid_hex=uid_hex, fingerprint="seed")
            # Two manual rows, both NULL — they must BOTH survive (NULLs distinct) and
            # stay NULL. If the recompute hashed them they would collide on the widened
            # index and the upgrade would fail outright.
            for _ in range(2):
                conn.execute(
                    text(
                        "INSERT INTO investment_transactions "
                        "(user_id, instrument_id, date, transaction_type, units, "
                        " amount_native_paise, fees_native_paise, fx_rate_to_inr) "
                        "VALUES (:u, :i, '2025-02-01', 'buy', 500000000, 50000, 0, 1000000)"
                    ).bindparams(u=uid_hex, i=inst_id)
                )

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")

        with eng.connect() as conn:
            nulls = conn.execute(
                text("SELECT COUNT(*) FROM investment_transactions WHERE fingerprint IS NULL")
            ).scalar()
        assert nulls == 2
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0027_downgrade_restores_the_old_key_and_is_reversible() -> None:
    """``alembic downgrade 0026`` from head drops ``occurrence``, re-narrows the unique
    index to 3 columns, and restores the separatorless fingerprints; re-upgrading returns
    to the ``\\x1f`` form. House precedent: each migration ships a downgrade test;
    ``test_migration_matches_models`` runs upgrade-only."""
    eng = _head_engine()
    try:
        uid_hex = uuid.uuid4().hex
        with eng.begin() as conn:
            inst_id, _ = _seed_investment_row(conn, uid_hex=uid_hex, fingerprint="whatever")

        assert _investment_uq_columns(eng) == [
            "user_id",
            "instrument_id",
            "fingerprint",
            "occurrence",
        ]

        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), "0026_rename_switch_pair_id_to_pair_id")

        insp = inspect(eng)
        assert "occurrence" not in {c["name"] for c in insp.get_columns("investment_transactions")}
        assert _investment_uq_columns(eng) == ["user_id", "instrument_id", "fingerprint"]
        with eng.connect() as conn:
            assert conn.execute(
                text("SELECT fingerprint FROM investment_transactions")
            ).scalar() == _investment_fp("", inst_id=inst_id)

        # Re-upgrade — up → down → up is clean.
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
        assert "occurrence" in _investment_uq_columns(eng)
        with eng.connect() as conn:
            assert conn.execute(
                text("SELECT fingerprint FROM investment_transactions")
            ).scalar() == _investment_fp("\x1f", inst_id=inst_id)
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0027_downgrade_refuses_to_merge_two_real_duplicates() -> None:
    """The conditional half of 0027's reversibility, asserted rather than just documented.

    Two rows that differ only by ``occurrence`` are legal at head. On downgrade they
    recompute to the SAME old fingerprint, and the re-narrowed 3-column index must
    REFUSE them — losing one would silently destroy a real transaction. The recompute
    runs before the narrowing precisely so this fails at CREATE UNIQUE INDEX."""
    eng = _head_engine()
    try:
        uid_hex = uuid.uuid4().hex
        with eng.begin() as conn:
            inst_id, _ = _seed_investment_row(conn, uid_hex=uid_hex, fingerprint="dupe")
            acct_id = conn.execute(
                text("SELECT id FROM investment_accounts WHERE user_id = :u").bindparams(u=uid_hex)
            ).scalar()
            # The second of two genuinely-distinct identical transactions.
            conn.execute(
                text(
                    "INSERT INTO investment_transactions "
                    "(user_id, account_id, instrument_id, date, transaction_type, units, "
                    " amount_native_paise, fees_native_paise, fx_rate_to_inr, "
                    " fingerprint, occurrence) "
                    "VALUES (:u, :a, :i, '2025-01-15', 'buy', 1000000000, 100000, 0, 1000000, "
                    " 'dupe', 1)"
                ).bindparams(u=uid_hex, a=acct_id, i=inst_id)
            )

        with pytest.raises(IntegrityError), eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), "0026_rename_switch_pair_id_to_pair_id")
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0022_downgrade_readds_note_and_is_reversible() -> None:
    """``alembic downgrade 0021`` from head re-adds ``transactions.note`` (leaving
    the label tables in place); re-upgrading drops it again — the drop is
    schema-reversible (data loss is by design). House precedent: each migration
    ships a downgrade test; ``test_migration_matches_models`` runs upgrade-only."""
    eng = _head_engine()
    try:
        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), "0021_add_transaction_labels")

        insp = inspect(eng)
        assert "note" in {c["name"] for c in insp.get_columns("transactions")}
        assert insp.has_table("labels")
        assert insp.has_table("transaction_labels")

        # Re-upgrade drops note again — up → down → up is clean.
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
        assert "note" not in {c["name"] for c in inspect(eng).get_columns("transactions")}
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0023_downgrade_drops_merchant_label_map() -> None:
    """``alembic downgrade 0022`` from head drops ``merchant_label_map``; re-upgrading
    re-creates it — the table is schema-reversible. House precedent: each migration
    ships a downgrade test; ``test_migration_matches_models`` runs upgrade-only."""
    eng = _head_engine()
    try:
        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), "0022_drop_transaction_note")

        assert not inspect(eng).has_table("merchant_label_map")

        # Re-upgrade re-creates it — up → down → up is clean.
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
        assert inspect(eng).has_table("merchant_label_map")
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0021_downgrade_drops_label_tables() -> None:
    """``alembic downgrade 0020`` from head drops both label tables (reverse FK
    order: transaction_labels before labels). ``note`` is back at 0020 because
    0022's downgrade re-added it before 0021's ran."""
    eng = _head_engine()
    try:
        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), "0020_widen_merchant_normalized")

        insp = inspect(eng)
        assert not insp.has_table("transaction_labels")
        assert not insp.has_table("labels")
        assert "note" in {c["name"] for c in insp.get_columns("transactions")}
    finally:
        eng.dispose()


def test_transaction_labels_timestamps_have_server_default() -> None:
    """A bare ``INSERT INTO transaction_labels`` (no created_at/updated_at) must
    succeed against the *migrated* schema.

    ``TimestampMixin`` now carries a Python-side default too
    (``base.utcnow_default``), so the ORM path — ``set_labels_on_transaction``'s
    ``session.add(TransactionLabel(...))`` — is covered either way. This test
    deliberately uses raw ``text()`` SQL, which bypasses the ORM entirely, so it
    guards the one thing the Python default cannot: that migration 0021 stamps
    ``server_default=now()`` for inserts that never touch SQLAlchemy's mapper.
    ``_snapshot`` does now compare DB-side defaults, so a dropped default is caught
    twice over — but only this test says *why* it must not be dropped.
    FK ON (make_engine default) → seed real parent rows.
    """
    eng = _head_engine()
    try:
        uid_hex = get_settings().v1_user_id.hex  # 0001 seeds this user
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO accounts (user_id, name, type) VALUES (:u, 'Acc', 'bank')"
                ).bindparams(u=uid_hex)
            )
            acct_id = conn.execute(text("SELECT id FROM accounts WHERE name='Acc'")).scalar()
            conn.execute(
                text(
                    "INSERT INTO transactions "
                    "(user_id, account_id, date, amount_paise, transaction_type, "
                    " merchant_normalized, fingerprint, source) "
                    "VALUES (:u, :a, '2026-01-01', -100, 'spend', 'm', 'fp-tl', 'manual')"
                ).bindparams(u=uid_hex, a=acct_id)
            )
            txn_id = conn.execute(
                text("SELECT id FROM transactions WHERE fingerprint='fp-tl'")
            ).scalar()
            conn.execute(
                text("INSERT INTO labels (user_id, name) VALUES (:u, 'x')").bindparams(u=uid_hex)
            )
            lbl_id = conn.execute(text("SELECT id FROM labels WHERE name='x'")).scalar()

            # The bare insert under test — no timestamps supplied.
            conn.execute(
                text(
                    "INSERT INTO transaction_labels (transaction_id, label_id, user_id) "
                    "VALUES (:t, :l, :u)"
                ).bindparams(t=txn_id, l=lbl_id, u=uid_hex)
            )
            row = conn.execute(
                text("SELECT created_at, updated_at FROM transaction_labels")
            ).first()

        assert row is not None
        assert row[0] is not None and row[1] is not None
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0028_backfills_origin_fingerprint_for_import_rows_only() -> None:
    """The only test that proves 0028's backfill ran, and that it is source-scoped.

    Seed two rows at 0027 — one ``source='import'``, one ``source='manual'`` — then
    upgrade. ADR-0007 rule 9: the imported row gets its own fingerprint frozen as
    provenance, the manual row stays NULL so its dedup key remains its own *current*
    assertion (a manual row has no external artifact to freeze).

    Also pins the documented caveat that ``source='import'`` is the best signal the
    schema carries but not exactly "produced by the statement importer" — a
    backup-restored row replays the exported ``source`` and is stamped here too.
    Harmless while ``origin_fingerprint == fingerprint``, which is why the migration
    docstring records it instead of inventing a column to distinguish them.
    """
    eng = _migration_engine()
    try:
        uid_hex = uuid.uuid4().hex
        with eng.begin() as conn:
            command.upgrade(
                _alembic_cfg(conn), "0027_investment_fingerprint_separator_and_occurrence"
            )
            conn.execute(text("INSERT INTO users (id) VALUES (:u)").bindparams(u=uid_hex))
            conn.execute(
                text(
                    "INSERT INTO accounts (user_id, name, type) "
                    "VALUES (:u, 'Axis CC', 'credit_card')"
                ).bindparams(u=uid_hex)
            )
            acct_id = conn.execute(text("SELECT id FROM accounts")).scalar()
            for merchant, source in (("amazon", "import"), ("chai", "manual")):
                conn.execute(
                    text(
                        "INSERT INTO transactions "
                        "(user_id, account_id, date, amount_paise, transaction_type, "
                        " merchant_normalized, fingerprint, source) "
                        "VALUES (:u, :a, '2025-01-10', -5000, 'spend', :m, :f, :s)"
                    ).bindparams(u=uid_hex, a=acct_id, m=merchant, f=f"fp-{merchant}", s=source)
                )

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")

        with eng.connect() as conn:
            stamped = dict(
                conn.execute(
                    text("SELECT merchant_normalized, origin_fingerprint FROM transactions")
                ).all()
            )
        assert stamped["amazon"] == "fp-amazon"
        assert stamped["chai"] is None
    finally:
        eng.dispose()


def _seed_refund_row(conn: object, *, uid_hex: str, acct_id: int) -> int:
    """A ``transaction_type='refund'`` row at 0028 — the pre-0029 shape this
    migration's data step retypes. Positive amount, per the F2/§F4a sign rule
    ``RawTransaction.__post_init__`` already enforced on ``refund`` rows, so
    0029's upgrade never has to re-sign anything, only retype."""
    conn.execute(  # ty: ignore[unresolved-attribute]
        text(
            "INSERT INTO transactions "
            "(user_id, account_id, date, amount_paise, transaction_type, "
            " merchant_normalized, fingerprint, source) "
            "VALUES (:u, :a, '2025-06-01', 5000, 'refund', 'swiggy', 'fp-refund-1', 'import')"
        ).bindparams(u=uid_hex, a=acct_id)
    )
    return conn.execute(  # ty: ignore[unresolved-attribute]
        text("SELECT id FROM transactions WHERE fingerprint = 'fp-refund-1'")
    ).scalar()


@pytest.mark.slow
def test_0029_recomputes_refund_rows_to_positive_spend() -> None:
    """The only test that proves 0029's data step actually ran (ADR-0009).

    Seed a ``transaction_type='refund'`` row at 0028, upgrade to head, and assert
    it now stores ``spend`` with the SAME positive ``amount_paise`` — a pure
    retype, no re-signing (the migration docstring's claim). Also asserts the
    narrowed CHECK actually rejects ``refund`` post-upgrade, so a future
    contributor can't silently widen the vocabulary back without this failing.
    """
    eng = _migration_engine()
    try:
        uid_hex = uuid.uuid4().hex
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "0028_add_origin_fingerprint")
            conn.execute(text("INSERT INTO users (id) VALUES (:u)").bindparams(u=uid_hex))
            conn.execute(
                text(
                    "INSERT INTO accounts (user_id, name, type) "
                    "VALUES (:u, 'Axis CC', 'credit_card')"
                ).bindparams(u=uid_hex)
            )
            acct_id = conn.execute(text("SELECT id FROM accounts")).scalar()
            txn_id = _seed_refund_row(conn, uid_hex=uid_hex, acct_id=acct_id)

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")

        with eng.connect() as conn:
            stored_type, stored_amount = conn.execute(
                text(
                    "SELECT transaction_type, amount_paise FROM transactions WHERE id = :t"
                ).bindparams(t=txn_id)
            ).one()
        assert stored_type == "spend"
        assert stored_amount == 5000  # unchanged — a pure retype, not a re-sign.

        # The narrowed CHECK is live: a raw 'refund' insert now fails outright.
        with pytest.raises(IntegrityError), eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO transactions "
                    "(user_id, account_id, date, amount_paise, transaction_type, "
                    " merchant_normalized, fingerprint, source) "
                    "VALUES (:u, :a, '2025-06-02', 1000, 'refund', 'x', 'fp-refund-2', 'import')"
                ).bindparams(u=uid_hex, a=acct_id)
            )
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0029_partial_index_predicate_survives_up_and_down() -> None:
    """``ix_transactions_user_confirmed_date`` keeps ``confirmed_at IS NOT NULL``
    through 0029's rebuild in BOTH directions.

    ``test_migration_matches_models`` does not compare partial-index WHERE
    clauses (see ``Transaction.__table_args__``), so this asserts it directly —
    mirroring ``test_partial_index_where_clause_preserved`` /
    ``test_0025_batch_rebuild_preserves_the_partial_index_predicate``, but
    checking the downgrade side too, since 0029's downgrade rebuilds the same
    table and could lose the predicate the same way the upgrade could.
    """
    eng = _head_engine()
    try:

        def _predicate_sql() -> str | None:
            with eng.connect() as conn:
                return conn.execute(
                    text(
                        "SELECT sql FROM sqlite_master WHERE type = 'index' "
                        "AND name = 'ix_transactions_user_confirmed_date'"
                    )
                ).scalar()

        sql = _predicate_sql()
        assert sql is not None
        assert "confirmed_at IS NOT NULL" in sql

        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), "0028_add_origin_fingerprint")
        sql = _predicate_sql()
        assert sql is not None, "downgrade dropped the partial index entirely"
        assert "confirmed_at IS NOT NULL" in sql, (
            f"downgrade rebuild lost the partial-index predicate: {sql}"
        )
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0029_downgrade_restores_refund_type_and_is_reversible() -> None:
    """``alembic downgrade 0028`` from head re-widens the CHECK and retypes every
    positive ``spend`` back to ``refund``; re-upgrading collapses it to ``spend``
    again. House precedent: each migration ships a downgrade test;
    ``test_migration_matches_models`` runs upgrade-only.

    Also the executable form of the module docstring's reconstructive-not-exact
    caveat: a positive spend that was NEVER a refund is indistinguishable from
    one that was, so the downgrade retypes it too. Out of scope to assert here
    (no such row is reachable pre-0029, since ``spend > 0`` 422ed before this
    migration) — the up→down→up round trip is what this test actually proves.
    """
    eng = _migration_engine()
    try:
        uid_hex = uuid.uuid4().hex
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "0028_add_origin_fingerprint")
            conn.execute(text("INSERT INTO users (id) VALUES (:u)").bindparams(u=uid_hex))
            conn.execute(
                text(
                    "INSERT INTO accounts (user_id, name, type) "
                    "VALUES (:u, 'Axis CC', 'credit_card')"
                ).bindparams(u=uid_hex)
            )
            acct_id = conn.execute(text("SELECT id FROM accounts")).scalar()
            txn_id = _seed_refund_row(conn, uid_hex=uid_hex, acct_id=acct_id)

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
        with eng.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT transaction_type FROM transactions WHERE id = :t").bindparams(
                        t=txn_id
                    )
                ).scalar()
                == "spend"
            )

        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), "0028_add_origin_fingerprint")
        with eng.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT transaction_type FROM transactions WHERE id = :t").bindparams(
                        t=txn_id
                    )
                ).scalar()
                == "refund"
            )

        # Re-upgrade — up → down → up is clean.
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
        with eng.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT transaction_type FROM transactions WHERE id = :t").bindparams(
                        t=txn_id
                    )
                ).scalar()
                == "spend"
            )
    finally:
        eng.dispose()


def test_merchant_dictionary_matches_migration_seed() -> None:
    """Sibling to ``test_provisioning_matches_migration_seed``: the migration-seeded v1 user's
    ``is_seeded`` aliases + ``hit_count=0`` map rows must equal what ``_MERCHANT_DICTIONARY``
    implies, keeping migration 0032's frozen literal and ``provisioning.py``'s tuple mutually
    guarding (Phase A5 trap 6) instead of registrants silently diverging from the demo user.
    """
    eng = _head_engine()
    try:
        session_factory = sessionmaker(bind=eng)
        with session_factory() as s:
            v1 = get_settings().v1_user_id
            cats = {
                c.name: c.id
                for c in s.scalars(
                    select(Category).where(Category.user_id == v1, Category.archived_at.is_(None))
                )
            }
            aliases = {
                (a.pattern, a.canonical)
                for a in s.scalars(
                    select(MerchantAlias).where(
                        MerchantAlias.user_id == v1, MerchantAlias.is_seeded.is_(True)
                    )
                )
            }
            zero_hit_rows = {
                (m.merchant_normalized, m.category_id)
                for m in s.scalars(
                    select(MerchantTagMap).where(
                        MerchantTagMap.user_id == v1, MerchantTagMap.hit_count == 0
                    )
                )
            }
        expected_aliases = {(p, c) for p, c, _ in _MERCHANT_DICTIONARY}
        expected_map = {(c, cats[name]) for _, c, name in _MERCHANT_DICTIONARY}
        assert aliases == expected_aliases
        assert zero_hit_rows == expected_map
    finally:
        eng.dispose()


# (name, color) — the pre-hierarchy flat default categories (0003/0008/0012/0018), used ONLY
# to hand-build "other_user" below as a pre-0033 registrant. These used to live in
# provisioning.py as ``_DEFAULT_SPEND_CATEGORIES`` / ``_DEFAULT_INCOME_CATEGORIES``, but that
# module's live shape has been the 2-level taxonomy since ADR-0012 — this test is now their
# only reader, so they moved here rather than surviving as dead exports (AGENTS.md §Surgical
# changes: full-text search found zero callers under app/).
_LEGACY_FLAT_SPEND_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("Food", "#d95926"),
    ("Groceries", "#6f9e15"),
    ("Transport", "#2a78d6"),
    ("Rent", "#6c5cd6"),
    ("Utilities", "#0e97c4"),
    ("Shopping", "#d55181"),
    ("Entertainment", "#b246c0"),
    ("Health", "#e34948"),
    ("Travel", "#0e9488"),
    ("Subscriptions", "#1baf7a"),
    ("EMI", "#c23b6b"),
    ("Investment", "#008300"),
    ("Other", "#94a3b8"),
)
_LEGACY_FLAT_INCOME_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("Salary", "#008300"),
    ("Freelancing", "#2a78d6"),
    ("Cashback", "#c98500"),
    ("Other", "#94a3b8"),
)


@pytest.mark.slow
def test_backfill_skips_demo_taught_merchants_and_is_user_scoped() -> None:
    """The realistic Phase A5 trap-1 scenario: an existing deployment already ran
    ``app.services.demo_seed.seed_demo_data`` (12 of this dictionary's canonicals collide with
    what it teaches, at real hit_counts, via the ordinary ``record_tag`` path) BEFORE ever
    seeing migration 0032. The backfill must not raise, must not disturb those 12 rows, and
    must seed a second, unrelated pre-existing user identically — "idempotent" (safe against
    pre-existing colliding data) + "user-scoped" in one test.
    """
    eng = _migration_engine()
    try:
        v1 = get_settings().v1_user_id
        v1_val = v1.hex if hasattr(v1, "hex") else str(v1)
        other_user_id = uuid.uuid4()
        collisions = {
            "swiggy",
            "zomato",
            "uber",
            "netflix",
            "spotify",
            "apollo pharmacy",
            "makemytrip",
            "big basket",
            "croma",
            "myntra",
            "bookmyshow",
            "airtel",
        }

        collision_legacy_cats = {
            "swiggy": "Food",
            "zomato": "Food",
            "uber": "Transport",
            "netflix": "Subscriptions",
            "spotify": "Subscriptions",
            "apollo pharmacy": "Health",
            "makemytrip": "Travel",
            "big basket": "Groceries",
            "croma": "Shopping",
            "myntra": "Shopping",
            "bookmyshow": "Entertainment",
            "airtel": "Utilities",
        }

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "0031_add_merchant_alias")
            cats = dict(
                conn.execute(
                    text("SELECT name, id FROM categories WHERE user_id = :u AND kind = 'spend'"),
                    {"u": v1_val},
                ).fetchall()
            )
            inserted_canonicals: set[str] = set()
            for canonical, legacy_cat_name in collision_legacy_cats.items():
                if canonical not in inserted_canonicals and legacy_cat_name in cats:
                    inserted_canonicals.add(canonical)
                    conn.execute(
                        text(
                            "INSERT INTO merchant_tag_map "
                            "(user_id, merchant_normalized, category_id, "
                            "hit_count, created_at, updated_at) "
                            "VALUES (:u, :m, :c, 5, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                        ),
                        {"u": v1_val, "m": canonical, "c": cats[legacy_cat_name]},
                    )
            conn.execute(
                text(
                    "INSERT INTO users (id, created_at, updated_at) "
                    "VALUES (:u, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"u": other_user_id.hex},
            )
            for name, color in _LEGACY_FLAT_SPEND_CATEGORIES:
                conn.execute(
                    text(
                        "INSERT INTO categories "
                        "(user_id, name, kind, color, is_seeded, created_at, updated_at) "
                        "VALUES (:u, :name, 'spend', :color, 1, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"u": other_user_id.hex, "name": name, "color": color},
                )
            for name, color in _LEGACY_FLAT_INCOME_CATEGORIES:
                conn.execute(
                    text(
                        "INSERT INTO categories "
                        "(user_id, name, kind, color, is_seeded, created_at, updated_at) "
                        "VALUES (:u, :name, 'income', :color, 1, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"u": other_user_id.hex, "name": name, "color": color},
                )

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")  # must not raise

        distinct_canonicals = {c for _, c, _ in _MERCHANT_DICTIONARY}
        session_factory = sessionmaker(bind=eng)
        with session_factory() as s:
            v1_maps = {
                m.merchant_normalized: m.hit_count
                for m in s.scalars(select(MerchantTagMap).where(MerchantTagMap.user_id == v1))
            }
            assert all(v1_maps[c] >= 1 for c in collisions)  # demo's learned rows untouched
            zero_hit = {k for k, v in v1_maps.items() if v == 0}
            assert zero_hit == distinct_canonicals - collisions

            other_maps = {
                m.merchant_normalized
                for m in s.scalars(
                    select(MerchantTagMap).where(MerchantTagMap.user_id == other_user_id)
                )
            }
            assert other_maps == distinct_canonicals
            other_aliases = {
                a.pattern
                for a in s.scalars(
                    select(MerchantAlias).where(MerchantAlias.user_id == other_user_id)
                )
            }
            assert other_aliases == {p for p, _, _ in _MERCHANT_DICTIONARY}
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0032_downgrade_preserves_pinned_seed_row() -> None:
    """``downgrade()`` must not delete a seed row the user has PINNED.

    ``hit_count == 0`` is not an exclusive seed marker on its own: ``pin_tag``
    deliberately does not bump ``hit_count`` on an existing row (a pin is an
    assertion, not an observed decision), so pinning a seeded merchant leaves the
    row at ``hit_count = 0, pinned = True`` — user-authored data that the
    unqualified ``DELETE FROM merchant_tag_map WHERE hit_count = 0`` silently
    discarded. Sibling to
    ``test_default_categories_downgrade_removes_seeded_only``.
    """
    eng = _head_engine()
    try:
        v1 = get_settings().v1_user_id
        session_factory = sessionmaker(bind=eng)
        with session_factory() as s:
            pinned = s.scalar(
                select(MerchantTagMap).where(
                    MerchantTagMap.user_id == v1,
                    MerchantTagMap.merchant_normalized == "netflix",
                )
            )
            assert pinned is not None and pinned.hit_count == 0
            pin_tag(
                s,
                user_id=v1,
                merchant_normalized="netflix",
                category_id=pinned.category_id,
            )
            s.commit()
            # The pin landed on the existing seed row without bumping hit_count —
            # the precondition this test exists for.
            s.refresh(pinned)
            assert (pinned.hit_count, pinned.pinned) == (0, True)

        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), "0031_add_merchant_alias")

        # Pinned row survived; every other zero-hit seed row is gone.
        with session_factory() as s:
            rows = {
                (m.merchant_normalized, m.hit_count, m.pinned)
                for m in s.scalars(select(MerchantTagMap).where(MerchantTagMap.user_id == v1))
            }
            survivor = ("netflix", 0, True)
            assert survivor in rows
            assert rows == {survivor}
    finally:
        eng.dispose()


@pytest.mark.slow
def test_seed_bound_to_archived_category_not_created_via_migration() -> None:
    """Phase A5 trap 3, retargeted for the hierarchy: ``uq_categories_active_user_name`` is a
    *partial* index, so an archived category can share a name with an active one. 0035
    re-points a legacy seed row from a flat category onto its fine-grained subcategory — an
    archived subcategory must not receive that re-point (it would bind a seed row to an
    archived category), without disturbing any other merchant's re-point.

    Archives "Mobile & Broadband" / "Cable & Satellite TV" — the actual subcategories 0035's
    dictionary names for airtel/bsnl/tata play/etc — between 0034 (which creates them,
    unarchived) and head, where 0035 runs the re-point. The ORIGINAL version of this test
    archived "Utilities" *before* 0034 even ran: that only re-proved 0032's pre-existing
    archived-category skip (unrelated to the hierarchy) and never exercised 0035 at all, since
    "Mobile & Broadband" / "Cable & Satellite TV" don't exist — let alone bind anything — until
    0034 creates the very row this test now archives.
    """
    eng = _migration_engine()
    try:
        v1 = get_settings().v1_user_id
        v1_val = v1.hex if hasattr(v1, "hex") else str(v1)
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "0034_seed_category_taxonomy")
            conn.execute(
                text(
                    "UPDATE categories SET archived_at = CURRENT_TIMESTAMP "
                    "WHERE user_id = :u AND kind = 'spend' "
                    "AND name IN ('Mobile & Broadband', 'Cable & Satellite TV')"
                ),
                {"u": v1_val},
            )
            utilities_id = conn.execute(
                text(
                    "SELECT id FROM categories WHERE user_id = :u "
                    "AND kind = 'spend' AND name = 'Utilities'"
                ),
                {"u": v1_val},
            ).scalar()

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")  # must not raise, must not dangle

        archived_target_canonicals = {
            canonical
            for _, canonical, name in _MERCHANT_DICTIONARY
            if name in ("Mobile & Broadband", "Cable & Satellite TV")
        }
        other_targets = {
            canonical: name
            for _, canonical, name in _MERCHANT_DICTIONARY
            if name not in ("Mobile & Broadband", "Cable & Satellite TV")
        }
        session_factory = sessionmaker(bind=eng)
        with session_factory() as s:
            cat_id_by_name = {
                c.name: c.id
                for c in s.scalars(
                    select(Category).where(
                        Category.user_id == v1,
                        Category.kind == "spend",
                        Category.archived_at.is_(None),
                    )
                )
            }
            v1_maps = {
                m.merchant_normalized: m.category_id
                for m in s.scalars(select(MerchantTagMap).where(MerchantTagMap.user_id == v1))
            }
            # Archived target: 0035 skipped the re-point, so the seed row stayed on the
            # legacy flat category it already pointed at — never on the archived subcategory,
            # never dangling.
            for canonical in archived_target_canonicals:
                assert v1_maps[canonical] == utilities_id, (
                    f"{canonical} should stay bound to legacy 'Utilities' while its target "
                    f"subcategory is archived, got category_id={v1_maps[canonical]}"
                )
            # Every other merchant still re-pointed to its real subcategory — the archived
            # pair didn't disturb 0035's loop for anyone else.
            for canonical, target_name in other_targets.items():
                assert v1_maps[canonical] == cat_id_by_name[target_name]
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0034_downgrade_preserves_referenced_and_user_authored_categories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``alembic downgrade 0033`` (0035's downgrade, a merchant_tag_map-only no-op
    here, then 0034's) must not destroy data — under REAL FK enforcement.

    The stairway test above is structural only and walks an empty DB, so it
    cannot see a downgrade orphan a transaction; this test seeds real referencing
    rows. Regression coverage for the two verified defects in the *original*
    ``downgrade()``: (2.2) an unscoped ``UPDATE categories SET parent_id = NULL``
    with no WHERE would have flattened a user-authored hierarchy the migration
    never created; (2.3) two unconditional DELETEs of seeded categories by name,
    with no check for referencing rows, would leave ``transactions`` /
    ``merchant_tag_map`` pointing at a row that no longer exists (or, under FK
    enforcement, simply raise).

    Drives the upgrade half through the CLI path against a real temp file — FK
    OFF there is deliberate (0033's now-populated-table recreate, see env.py) —
    then reopens a fresh ``make_engine`` connection (FK ON at connect, the
    opposite of this file's throwaway ``_migration_engine``) for seeding, the
    downgrade under test, and every assertion, so a regression here surfaces as
    a genuine ``IntegrityError`` or a caught dangling FK, not a silently-passing
    test.
    """
    import app.core.config as config_mod

    uid_hex = uuid.UUID("00000000-0000-0000-0000-000000000001").hex  # 0001 seeds this user
    db_url = f"sqlite:///{(tmp_path / 'cat_downgrade.db').as_posix()}"
    monkeypatch.setattr(
        config_mod, "get_settings", lambda: config_mod.Settings(database_url=db_url)
    )

    def _cli_cfg() -> Config:
        cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
        return cfg

    # Pinned to 0036, not head: the subject is 0035/0034's downgrade. 0037's downgrade
    # batch-rebuilds referenced tables, which (ADR-0001 rule 4b) only ever runs FK-off,
    # so it cannot ride along on this FK-ON connection.
    command.upgrade(_cli_cfg(), "0036_add_guest_user_fields")

    eng = make_engine(db_url)  # FK ON at connect — the real enforcement this test needs.
    try:
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO accounts (user_id, name, type) "
                    "VALUES (:u, 'Axis CC', 'credit_card')"
                ).bindparams(u=uid_hex)
            )
            acct_id = conn.execute(text("SELECT id FROM accounts")).scalar()

            sub_id = conn.execute(
                text(
                    "SELECT id FROM categories WHERE user_id = :u AND kind = 'spend' "
                    "AND name = 'Coffee & Tea' AND archived_at IS NULL"
                ).bindparams(u=uid_hex)
            ).scalar()
            income_parent_id = conn.execute(
                text(
                    "SELECT id FROM categories WHERE user_id = :u AND kind = 'income' "
                    "AND name = 'Income' AND archived_at IS NULL"
                ).bindparams(u=uid_hex)
            ).scalar()
            assert sub_id is not None and income_parent_id is not None

            # One transaction tagged to a seeded SUBCATEGORY, one to the seeded
            # "Income" PARENT directly.
            conn.execute(
                text(
                    "INSERT INTO transactions "
                    "(user_id, account_id, date, amount_paise, transaction_type, "
                    " merchant_normalized, fingerprint, source, category_id) "
                    "VALUES (:u, :a, '2025-01-10', -500, 'spend', 'starbucks', 'fp-sub', "
                    " 'manual', :c)"
                ).bindparams(u=uid_hex, a=acct_id, c=sub_id)
            )
            conn.execute(
                text(
                    "INSERT INTO transactions "
                    "(user_id, account_id, date, amount_paise, transaction_type, "
                    " merchant_normalized, fingerprint, source, category_id) "
                    "VALUES (:u, :a, '2025-01-15', 50000, 'income', 'employer', 'fp-inc', "
                    " 'manual', :c)"
                ).bindparams(u=uid_hex, a=acct_id, c=income_parent_id)
            )

            # A hand-created parent/child pair 0034 never touched and has no
            # business reparenting or deleting.
            conn.execute(
                text(
                    "INSERT INTO categories (user_id, name, kind, is_seeded) "
                    "VALUES (:u, 'My Parent', 'spend', 0)"
                ).bindparams(u=uid_hex)
            )
            user_parent_id = conn.execute(
                text("SELECT id FROM categories WHERE name = 'My Parent'")
            ).scalar()
            conn.execute(
                text(
                    "INSERT INTO categories (user_id, name, kind, is_seeded, parent_id) "
                    "VALUES (:u, 'My Child', 'spend', 0, :p)"
                ).bindparams(u=uid_hex, p=user_parent_id)
            )
            user_child_id = conn.execute(
                text("SELECT id FROM categories WHERE name = 'My Child'")
            ).scalar()

        # The downgrade under test — real FK enforcement throughout (no CLI-path
        # FK-off: this connection's listener turned it ON at connect).
        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), "0033_add_category_parent_id")

        with eng.connect() as conn:
            # (b) no FK dangles — asked of SQLite directly rather than inferred.
            violations = conn.execute(text("PRAGMA foreign_key_check")).all()
            assert violations == [], f"dangling FK(s) after downgrade: {violations}"

            # (a) both transactions still resolve a category (archived is fine —
            # deleted, i.e. the id no longer existing, is not).
            sub_resolves, income_resolves = conn.execute(
                text(
                    "SELECT "
                    "(SELECT COUNT(*) FROM categories WHERE id = "
                    " (SELECT category_id FROM transactions WHERE fingerprint = 'fp-sub')), "
                    "(SELECT COUNT(*) FROM categories WHERE id = "
                    " (SELECT category_id FROM transactions WHERE fingerprint = 'fp-inc'))"
                )
            ).one()
            assert sub_resolves == 1
            assert income_resolves == 1

            # (c) the user-authored parent_id survived 2.2's scoping.
            child_parent_id = conn.execute(
                text("SELECT parent_id FROM categories WHERE id = :c").bindparams(c=user_child_id)
            ).scalar()
            assert child_parent_id == user_parent_id
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0037_refuses_to_run_over_a_cross_user_reference() -> None:
    """ADR-0003 rule 10: 0037 counts cross-user links before adding the composite FKs.

    SQLite migrates with FKs off, so without the preflight a corrupt row would be copied into
    the rebuilt table silently (Postgres would refuse it). The error names table, column and
    count only — never a row id or anything a user typed.
    """
    eng = _migration_engine()
    try:
        v1 = get_settings().v1_user_id
        v1_val = v1.hex if hasattr(v1, "hex") else str(v1)
        other_val = uuid.UUID("00000000-0000-0000-0000-0000000000a1").hex
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "0036_add_guest_user_fields")
            conn.execute(text("INSERT INTO users (id) VALUES (:u)"), {"u": other_val})
            their_parent = conn.execute(
                text(
                    "INSERT INTO categories (user_id, name) VALUES (:u, 'Their Secret') "
                    "RETURNING id"
                ),
                {"u": other_val},
            ).scalar_one()
            conn.execute(
                text("INSERT INTO categories (user_id, name, parent_id) VALUES (:u, 'Mine', :p)"),
                {"u": v1_val, "p": their_parent},
            )

        with pytest.raises(RuntimeError) as exc, eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
        message = str(exc.value)
        assert "categories.parent_id: 1 row(s) reference another user's categories" in message
        assert "Their Secret" not in message
    finally:
        eng.dispose()


def _table_ddl(eng: Engine, table: str) -> str:
    with eng.connect() as conn:
        return (
            conn.execute(
                text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :t"),
                {"t": table},
            ).scalar()
            or ""
        )


def _confirmed_index_sql(eng: Engine) -> str:
    with eng.connect() as conn:
        return (
            conn.execute(
                text(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' "
                    "AND name = 'ix_transactions_user_confirmed_date'"
                )
            ).scalar()
            or ""
        )


@pytest.mark.slow
def test_0038_downgrade_is_reversible() -> None:
    """``alembic downgrade 0037`` from head restores the doubled CHECK names, drops
    ``paired_is_transfer``, ``pre_link_type`` and ``refund_of_id`` (with its FK) and puts back
    the ``fx_rate_to_inr`` default; re-upgrading reverses each. Both directions rebuild
    ``transactions``, so the partial-index predicate is checked on each side too."""
    eng = _head_engine()
    try:

        def _assert_head() -> None:
            txn_ddl = _table_ddl(eng, "transactions")
            inv_ddl = _table_ddl(eng, "investment_transactions")
            assert "CONSTRAINT ck_transactions_no_self_pair CHECK" in txn_ddl
            assert "ck_transactions_ck_" not in txn_ddl
            assert "CONSTRAINT ck_investment_transactions_no_self_pair CHECK" in inv_ddl
            assert "ck_investment_transactions_ck_" not in inv_ddl
            assert "confirmed_at IS NOT NULL" in _confirmed_index_sql(eng)
            assert _fx_default() is None
            assert {"pre_link_type", "refund_of_id"} <= _txn_columns()

        def _txn_columns() -> set[str]:
            return {c["name"] for c in inspect(eng).get_columns("transactions")}

        def _fx_default() -> object:
            cols = inspect(eng).get_columns("investment_transactions")
            return next(c for c in cols if c["name"] == "fx_rate_to_inr").get("default")

        _assert_head()

        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), "0037_composite_same_user_fks")
        txn_ddl = _table_ddl(eng, "transactions")
        inv_ddl = _table_ddl(eng, "investment_transactions")
        assert "ck_transactions_ck_transactions_no_self_pair" in txn_ddl
        assert "ck_investment_transactions_ck_investment_transactions_no_self_pair" in inv_ddl
        assert "confirmed_at IS NOT NULL" in _confirmed_index_sql(eng)

        assert "paired_is_transfer" not in txn_ddl
        assert _fx_default() == "1000000"
        assert not {"pre_link_type", "refund_of_id"} & _txn_columns()
        fk_names = {f["name"] for f in inspect(eng).get_foreign_keys("transactions")}
        assert "fk_transactions_refund_of_id_transactions" not in fk_names

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
        _assert_head()
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0038_refuses_to_run_over_a_paired_non_transfer_row() -> None:
    """ADR-0002 rule 8: 0038 counts paired non-transfer rows before adding CHECK
    ``paired_is_transfer`` and aborts with the count only — no id, no merchant text."""
    eng = _migration_engine()
    try:
        v1 = get_settings().v1_user_id
        uid = v1.hex if hasattr(v1, "hex") else str(v1)
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "0037_composite_same_user_fks")
            acct = conn.execute(
                text(
                    "INSERT INTO accounts (user_id, name, type) VALUES (:u, 'Acc', 'bank') "
                    "RETURNING id"
                ),
                {"u": uid},
            ).scalar_one()
            ids = []
            for txn_type, amount, fp in (("spend", -5000, "fp-a"), ("transfer", 5000, "fp-b")):
                ids.append(
                    conn.execute(
                        text(
                            "INSERT INTO transactions (user_id, account_id, date, amount_paise, "
                            " transaction_type, merchant_normalized, fingerprint, source) "
                            "VALUES (:u, :a, '2025-01-10', :amt, :t, 'secret merchant', :fp, "
                            " 'import') RETURNING id"
                        ),
                        {"u": uid, "a": acct, "amt": amount, "t": txn_type, "fp": fp},
                    ).scalar_one()
                )
            conn.execute(
                text("UPDATE transactions SET transfer_pair_id = :p WHERE id = :i"),
                [{"p": ids[1], "i": ids[0]}, {"p": ids[0], "i": ids[1]}],
            )

        with pytest.raises(RuntimeError) as exc, eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
        message = str(exc.value)
        assert message.startswith("1 paired transaction row(s) are not of type 'transfer'")
        assert "secret merchant" not in message
    finally:
        eng.dispose()


# The fallback-roots / cashback-to-spend revision. Written once so a re-chain of the revision
# touches only this constant; the pre-state is read from the script directory, never named.
_REV_FALLBACK_CASHBACK = "0039_fallback_roots_and_cashback_spend"


def _before_fallback_cashback() -> str:
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg).get_revision(_REV_FALLBACK_CASHBACK)
    assert script is not None
    assert isinstance(script.down_revision, str)
    return script.down_revision


@pytest.mark.slow
def test_fallback_migration_leaves_one_active_root_other_per_kind() -> None:
    """ADR-0012 rule 8: after the upgrade each user has exactly one active root "Other" per
    kind. The v1 user's income "Other" (a child of "Income" since 0034, colour nulled) is
    re-rooted and gets the seed hex; a user whose spend "Other" is archived and whose income
    "Other" was renamed away gets fresh roots, and the renamed row is left untouched."""
    eng = _migration_engine()
    try:
        v1_val = get_settings().v1_user_id.hex
        other_val = uuid.uuid4().hex
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _before_fallback_cashback())
            income_other = conn.execute(
                text(
                    "SELECT parent_id, color FROM categories "
                    "WHERE user_id = :u AND name = 'Other' AND kind = 'income'"
                ),
                {"u": v1_val},
            ).one()
            assert income_other.parent_id is not None, "pre-state: income Other is a child"
            assert income_other.color is None

            conn.execute(text("INSERT INTO users (id) VALUES (:u)"), {"u": other_val})
            conn.execute(
                text(
                    "INSERT INTO categories (user_id, name, kind, is_seeded, archived_at, color) "
                    "VALUES (:u, 'Other', 'spend', 1, '2026-01-01 00:00:00', '#94a3b8')"
                ),
                {"u": other_val},
            )
            renamed_id = conn.execute(
                text(
                    "INSERT INTO categories (user_id, name, kind, is_seeded, color) "
                    "VALUES (:u, 'Misc income', 'income', 1, '#123456') RETURNING id"
                ),
                {"u": other_val},
            ).scalar_one()

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")

        with eng.connect() as conn:
            for uid in (v1_val, other_val):
                for kind in ("spend", "income"):
                    rows = conn.execute(
                        text(
                            "SELECT parent_id, color FROM categories WHERE user_id = :u "
                            "AND name = 'Other' AND kind = :k AND archived_at IS NULL"
                        ),
                        {"u": uid, "k": kind},
                    ).all()
                    assert len(rows) == 1, (uid, kind)
                    assert rows[0].parent_id is None, (uid, kind)
                    assert rows[0].color == "#94a3b8", (uid, kind)
            renamed = conn.execute(
                text("SELECT name, color, parent_id, archived_at FROM categories WHERE id = :i"),
                {"i": renamed_id},
            ).one()
            assert tuple(renamed) == ("Misc income", "#123456", None, None)
    finally:
        eng.dispose()


def _seed_cashback_income_rows(conn: object) -> dict[str, int]:
    """At the pre-revision: the v1 user's account plus three income rows — two in the seeded
    income "Cashback" (a credit and a reversal), one in "Salary". Returns fingerprint → id."""
    v1_val = get_settings().v1_user_id.hex
    conn.execute(  # ty: ignore[unresolved-attribute]
        text("INSERT INTO accounts (user_id, name, type) VALUES (:u, 'Axis CC', 'credit_card')"),
        {"u": v1_val},
    )
    acct_id = conn.execute(text("SELECT id FROM accounts")).scalar()  # ty: ignore[unresolved-attribute]
    ids: dict[str, int] = {}
    for fingerprint, amount, category in (
        ("fp-cb-credit", 5000, "Cashback"),
        ("fp-cb-reversal", -1000, "Cashback"),
        ("fp-salary", 50000, "Salary"),
    ):
        ids[fingerprint] = conn.execute(  # ty: ignore[unresolved-attribute]
            text(
                "INSERT INTO transactions "
                "(user_id, account_id, date, amount_paise, transaction_type, "
                " merchant_normalized, fingerprint, source, category_id) "
                "VALUES (:u, :a, '2026-03-10', :amt, 'income', 'axis bank', :f, 'import', "
                " (SELECT id FROM categories WHERE user_id = :u AND name = :c "
                "  AND kind = 'income' AND archived_at IS NULL)) RETURNING id"
            ),
            {"u": v1_val, "a": acct_id, "amt": amount, "f": fingerprint, "c": category},
        ).scalar_one()
    return ids


@pytest.mark.slow
def test_cashback_rows_move_to_spend() -> None:
    """ADR-0013 rule 5: income rows in the income "Cashback" become ``spend`` rows of the same
    sign in an active root spend "Cashback" — a pure retype, fingerprints untouched — and the
    income "Cashback" is archived. A Salary row is not touched."""
    eng = _migration_engine()
    try:
        v1_val = get_settings().v1_user_id.hex
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _before_fallback_cashback())
            ids = _seed_cashback_income_rows(conn)
            salary_cat = conn.execute(
                text("SELECT category_id FROM transactions WHERE id = :i"),
                {"i": ids["fp-salary"]},
            ).scalar_one()

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")

        with eng.connect() as conn:
            spend_cashback = conn.execute(
                text(
                    "SELECT id, parent_id, color, is_seeded FROM categories WHERE user_id = :u "
                    "AND name = 'Cashback' AND kind = 'spend' AND archived_at IS NULL"
                ),
                {"u": v1_val},
            ).one()
            assert spend_cashback.parent_id is None
            assert spend_cashback.color == "#c98500"
            assert spend_cashback.is_seeded

            rows = {
                r.fingerprint: r
                for r in conn.execute(
                    text(
                        "SELECT fingerprint, transaction_type, amount_paise, category_id "
                        "FROM transactions"
                    )
                )
            }
            assert tuple(rows["fp-cb-credit"])[1:] == ("spend", 5000, spend_cashback.id)
            assert tuple(rows["fp-cb-reversal"])[1:] == ("spend", -1000, spend_cashback.id)
            assert tuple(rows["fp-salary"])[1:] == ("income", 50000, salary_cat)

            income_cashback = conn.execute(
                text(
                    "SELECT archived_at FROM categories WHERE user_id = :u "
                    "AND name = 'Cashback' AND kind = 'income'"
                ),
                {"u": v1_val},
            ).all()
            assert len(income_cashback) == 1
            assert income_cashback[0].archived_at is not None
    finally:
        eng.dispose()


@pytest.mark.slow
def test_cashback_migration_downgrade_moves_cashback_back_to_income() -> None:
    """The downgrade is reconstructive: spend-Cashback rows go back to ``income`` (sign kept)
    in the un-archived income "Cashback", and the seeded spend "Cashback", now unreferenced,
    is deleted."""
    eng = _migration_engine()
    try:
        v1_val = get_settings().v1_user_id.hex
        before = _before_fallback_cashback()
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), before)
            ids = _seed_cashback_income_rows(conn)
            income_cashback_id = conn.execute(
                text("SELECT category_id FROM transactions WHERE id = :i"),
                {"i": ids["fp-cb-credit"]},
            ).scalar_one()

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), before)

        with eng.connect() as conn:
            rows = {
                r.fingerprint: tuple(r)[1:]
                for r in conn.execute(
                    text(
                        "SELECT fingerprint, transaction_type, amount_paise, category_id "
                        "FROM transactions"
                    )
                )
            }
            assert rows["fp-cb-credit"] == ("income", 5000, income_cashback_id)
            assert rows["fp-cb-reversal"] == ("income", -1000, income_cashback_id)
            assert (
                conn.execute(
                    text("SELECT archived_at FROM categories WHERE id = :i"),
                    {"i": income_cashback_id},
                ).scalar_one()
                is None
            )
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM categories WHERE user_id = :u "
                        "AND name = 'Cashback' AND kind = 'spend'"
                    ),
                    {"u": v1_val},
                ).scalar_one()
                == 0
            )
    finally:
        eng.dispose()


# ---------------------------------------------------------------------------
# 0040_add_realised_lots (ADR-0015 rule 5). Written once; the pre-state is derived.
# ---------------------------------------------------------------------------

_REV_REALISED_LOTS = "0040_add_realised_lots"


def _before_realised_lots() -> str:
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg).get_revision(_REV_REALISED_LOTS)
    assert script is not None
    assert isinstance(script.down_revision, str)
    return script.down_revision


def _assert_realised_lots_schema(conn) -> None:  # type: ignore[no-untyped-def]
    insp = inspect(conn)
    assert "realised_lots" in insp.get_table_names()
    assert "ix_realised_lots_user_buy" in {i["name"] for i in insp.get_indexes("realised_lots")}
    assert "uq_realised_lots_user_sell_buy" in {
        u["name"] for u in insp.get_unique_constraints("realised_lots")
    }
    fks = {f["name"]: f for f in insp.get_foreign_keys("realised_lots")}
    assert set(fks) == {
        "fk_realised_lots_sell_txn_id_investment_transactions",
        "fk_realised_lots_buy_txn_id_investment_transactions",
    }
    assert all(f["options"].get("ondelete") == "CASCADE" for f in fks.values())
    assert {c["name"] for c in insp.get_check_constraints("realised_lots")} == {
        f"ck_realised_lots_{s}"
        for s in (
            "positive_units",
            "non_negative_cost",
            "non_negative_proceeds",
            "non_negative_sell_fees",
            "buy_not_after_sell",
        )
    }


@pytest.mark.slow
def test_realised_lots_migration_is_reversible() -> None:
    eng = _migration_engine()
    try:
        before = _before_realised_lots()
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), before)
            assert "realised_lots" not in inspect(conn).get_table_names()
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _REV_REALISED_LOTS)
        with eng.connect() as conn:
            _assert_realised_lots_schema(conn)
        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), before)
        with eng.connect() as conn:
            assert "realised_lots" not in inspect(conn).get_table_names()
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _REV_REALISED_LOTS)
        with eng.connect() as conn:
            _assert_realised_lots_schema(conn)
    finally:
        eng.dispose()


# The portable-origin-inputs revision. Written once; the pre-state is read from the script
# directory, never named.
_REV_PORTABLE_ORIGIN = "0041_portable_origin_inputs"


def _before_portable_origin() -> str:
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg).get_revision(_REV_PORTABLE_ORIGIN)
    assert script is not None
    assert isinstance(script.down_revision, str)
    return script.down_revision


def _insert_origin_row(conn: object, acct_id: int, fp: str, origin: str | None) -> None:
    conn.execute(  # ty: ignore[unresolved-attribute]
        text(
            "INSERT INTO transactions "
            "(user_id, account_id, date, amount_paise, transaction_type, merchant_normalized, "
            " fingerprint, origin_fingerprint, source) "
            "VALUES (:u, :a, '2026-03-10', -100, 'spend', 'shop', :f, :o, 'import')"
        ),
        {"u": get_settings().v1_user_id.hex, "a": acct_id, "f": fp, "o": origin},
    )


@pytest.mark.slow
def test_portable_origin_migration_nulls_edited_origins_only() -> None:
    """An unedited import row (origin == fingerprint) keeps its origin; an edited one (origin !=
    fingerprint, inputs unrecoverable) and a manual one end with NULL; the new columns are NULL."""
    eng = _migration_engine()
    try:
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _before_portable_origin())
            conn.execute(
                text(
                    "INSERT INTO accounts (user_id, name, type) "
                    "VALUES (:u, 'Axis CC', 'credit_card')"
                ),
                {"u": get_settings().v1_user_id.hex},
            )
            acct_id = conn.execute(text("SELECT id FROM accounts")).scalar_one()
            _insert_origin_row(conn, acct_id, "fp-unedited", "fp-unedited")
            _insert_origin_row(conn, acct_id, "fp-edited", "fp-before-the-edit")
            _insert_origin_row(conn, acct_id, "fp-manual", None)

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")

        with eng.connect() as conn:
            rows = {
                r.fingerprint: r
                for r in conn.execute(
                    text(
                        "SELECT fingerprint, origin_fingerprint, origin_date, "
                        "origin_amount_paise, origin_merchant_normalized, origin_account_id "
                        "FROM transactions"
                    )
                )
            }
        assert rows["fp-unedited"].origin_fingerprint == "fp-unedited"
        assert rows["fp-edited"].origin_fingerprint is None
        assert rows["fp-manual"].origin_fingerprint is None
        for row in rows.values():
            assert tuple(row)[2:] == (None, None, None, None)
    finally:
        eng.dispose()


@pytest.mark.slow
def test_portable_origin_migration_downgrade_is_reversible() -> None:
    """At head a row with origin inputs downgrades cleanly (columns gone, origin_fingerprint
    intact) and the revision applies again."""
    eng = _migration_engine()
    try:
        before = _before_portable_origin()
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
            conn.execute(
                text(
                    "INSERT INTO accounts (user_id, name, type) "
                    "VALUES (:u, 'Axis CC', 'credit_card')"
                ),
                {"u": get_settings().v1_user_id.hex},
            )
            acct_id = conn.execute(text("SELECT id FROM accounts")).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO transactions "
                    "(user_id, account_id, date, amount_paise, transaction_type, "
                    " merchant_normalized, fingerprint, origin_fingerprint, source, "
                    " origin_date, origin_amount_paise, origin_merchant_normalized, "
                    " origin_account_id) "
                    "VALUES (:u, :a, '2026-03-10', -200, 'spend', 'shop', 'fp-now', 'fp-then', "
                    " 'import', '2026-03-09', -100, 'shop', :a)"
                ),
                {"u": get_settings().v1_user_id.hex, "a": acct_id},
            )

        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), before)
        with eng.connect() as conn:
            columns = {c["name"] for c in inspect(conn).get_columns("transactions")}
            assert not {c for c in columns if c.startswith("origin_")} - {"origin_fingerprint"}
            assert (
                conn.execute(text("SELECT origin_fingerprint FROM transactions")).scalar_one()
                == "fp-then"
            )

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
        with eng.connect() as conn:
            assert "origin_date" in {c["name"] for c in inspect(conn).get_columns("transactions")}
    finally:
        eng.dispose()


_REV_DROP_ERROR_MESSAGE = "0042_drop_import_batch_error_message"


def _down_revision_of(revision: str) -> str:
    cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg).get_revision(revision)
    assert script is not None
    assert isinstance(script.down_revision, str)
    return script.down_revision


@pytest.mark.slow
def test_drop_import_batch_error_message_is_reversible() -> None:
    """The drop keeps the batch row and its inbound FK from ``transactions`` intact, the
    downgrade restores a nullable column, and up -> down -> up is clean."""
    eng = _migration_engine()
    try:
        v1_val = get_settings().v1_user_id.hex
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _down_revision_of(_REV_DROP_ERROR_MESSAGE))
            assert "error_message" in {
                c["name"] for c in inspect(conn).get_columns("import_batches")
            }
            conn.execute(
                text(
                    "INSERT INTO accounts (user_id, name, type, currency, opening_balance_paise) "
                    "VALUES (:u, 'Axis CC', 'credit_card', 'INR', 0)"
                ),
                {"u": v1_val},
            )
            account_id = conn.execute(text("SELECT id FROM accounts")).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO import_batches (user_id, account_id, source_file_hash, "
                    "parser_name, status) VALUES (:u, :a, :h, 'axis_cc', 'completed')"
                ),
                {"u": v1_val, "a": account_id, "h": "0" * 64},
            )
            batch_id = conn.execute(text("SELECT id FROM import_batches")).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO transactions (user_id, account_id, date, amount_paise, "
                    "merchant_raw, merchant_normalized, transaction_type, source, "
                    "import_batch_id, fingerprint, occurrence) VALUES (:u, :a, '2026-03-05', "
                    "-100, 'X', 'x', 'spend', 'import', :b, :f, 1)"
                ),
                {"u": v1_val, "a": account_id, "b": batch_id, "f": "f" * 64},
            )

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _REV_DROP_ERROR_MESSAGE)
        insp = inspect(eng)
        assert "error_message" not in {c["name"] for c in insp.get_columns("import_batches")}
        assert "uq_import_batches_id_user" in {
            u["name"] for u in insp.get_unique_constraints("import_batches")
        } | {i["name"] for i in insp.get_indexes("import_batches")}
        with eng.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM import_batches")).scalar_one() == 1
            assert conn.execute(text("PRAGMA foreign_key_check")).all() == []

        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), _down_revision_of(_REV_DROP_ERROR_MESSAGE))
        cols = {c["name"]: c for c in inspect(eng).get_columns("import_batches")}
        assert cols["error_message"]["nullable"] is True
        with eng.connect() as conn:
            assert (
                conn.execute(text("SELECT error_message FROM import_batches")).scalar_one() is None
            )

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
        assert "error_message" not in {
            c["name"] for c in inspect(eng).get_columns("import_batches")
        }
    finally:
        eng.dispose()


_REV_REALISED_LOT_KIND = "0043_realised_lot_kind"


@pytest.mark.slow
def test_realised_lot_kind_migration_is_reversible() -> None:
    """Existing rows become ``fifo``; the column ends with no default; the CHECKs exist; a
    writer that omits ``kind`` fails NOT NULL; the downgrade drops it and keeps the row."""
    original_checks = {
        f"ck_realised_lots_{s}"
        for s in (
            "positive_units",
            "non_negative_cost",
            "non_negative_proceeds",
            "non_negative_sell_fees",
            "buy_not_after_sell",
        )
    }
    insert = (
        "INSERT INTO realised_lots (user_id, sell_txn_id, buy_txn_id, units, "
        "cost_native_paise, proceeds_native_paise, sell_fees_native_paise, buy_date, "
        "sell_date) VALUES (:u, 1, 2, 100000000, 1, 1, 0, '2026-01-01', '2026-02-01')"
    )
    eng = _migration_engine()
    try:
        v1_val = get_settings().v1_user_id.hex
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _down_revision_of(_REV_REALISED_LOT_KIND))
            conn.execute(text(insert), {"u": v1_val})

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _REV_REALISED_LOT_KIND)
        insp = inspect(eng)
        cols = {c["name"]: c for c in insp.get_columns("realised_lots")}
        assert cols["kind"]["default"] is None
        assert cols["kind"]["nullable"] is False
        assert {c["name"] for c in insp.get_check_constraints("realised_lots")} == (
            original_checks
            | {"ck_realised_lots_realised_lot_kind", "ck_realised_lots_intraday_same_day"}
        )
        with eng.connect() as conn:
            assert conn.execute(text("SELECT kind FROM realised_lots")).scalar_one() == "fifo"
        with pytest.raises(IntegrityError), eng.begin() as conn:
            conn.execute(text(insert.replace("(:u, 1, 2,", "(:u, 1, 3,")), {"u": v1_val})

        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), _down_revision_of(_REV_REALISED_LOT_KIND))
        insp = inspect(eng)
        assert "kind" not in {c["name"] for c in insp.get_columns("realised_lots")}
        assert {c["name"] for c in insp.get_check_constraints("realised_lots")} == original_checks
        with eng.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM realised_lots")).scalar_one() == 1

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
    finally:
        eng.dispose()


# The pre-link-category revision. Written once; the pre-state comes from the script directory.
_REV_PRE_LINK_CATEGORY = "0044_pre_link_category"


def _seed_linked_categorised_pair(conn: Connection) -> tuple[int, int, int]:
    """An account, a category and two paired transfer legs; returns (category, leg a, leg b)."""
    uid = get_settings().v1_user_id.hex
    conn.execute(
        text("INSERT INTO accounts (user_id, name, type) VALUES (:u, 'Bank', 'bank')"),
        {"u": uid},
    )
    acct = conn.execute(text("SELECT id FROM accounts")).scalar_one()
    conn.execute(
        text(
            "INSERT INTO categories (user_id, name, color) VALUES (:u, 'Pre-link test', '#2a78d6')"
        ),
        {"u": uid},
    )
    cat = conn.execute(text("SELECT id FROM categories WHERE name = 'Pre-link test'")).scalar_one()
    ids = []
    for fp, amt in (("fp-a", -100), ("fp-b", 100)):
        conn.execute(
            text(
                "INSERT INTO transactions (user_id, account_id, date, amount_paise, "
                "transaction_type, merchant_normalized, fingerprint, source) "
                "VALUES (:u, :a, '2026-03-10', :p, 'transfer', 'x', :f, 'import')"
            ),
            {"u": uid, "a": acct, "p": amt, "f": fp},
        )
        ids.append(
            conn.execute(
                text("SELECT id FROM transactions WHERE fingerprint = :f"), {"f": fp}
            ).scalar_one()
        )
    a, b = ids
    for me, other in ((a, b), (b, a)):
        conn.execute(
            text("UPDATE transactions SET transfer_pair_id = :o, category_id = :c WHERE id = :i"),
            {"o": other, "c": cat, "i": me},
        )
    return cat, a, b


@pytest.mark.slow
def test_pre_link_category_migration_adds_an_empty_column() -> None:
    """The column lands NULL everywhere; linked rows keep their category (no backfill)."""
    eng = _migration_engine()
    try:
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _down_revision_of(_REV_PRE_LINK_CATEGORY))
            cat, _, _ = _seed_linked_categorised_pair(conn)
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _REV_PRE_LINK_CATEGORY)
        with eng.connect() as conn:
            assert "pre_link_category_id" in {
                c["name"] for c in inspect(conn).get_columns("transactions")
            }
            rows = conn.execute(
                text("SELECT category_id, pre_link_category_id FROM transactions")
            ).all()
        assert [tuple(r) for r in rows] == [(cat, None), (cat, None)]
    finally:
        eng.dispose()


@pytest.mark.slow
def test_pre_link_category_migration_downgrade_folds_back_and_is_reversible() -> None:
    """Downgrade moves each saved category back onto its row in one UPDATE (the CHECK is still
    enforced then), drops column and CHECK, and the revision applies again."""
    eng = _migration_engine()
    try:
        pre = _down_revision_of(_REV_PRE_LINK_CATEGORY)
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _REV_PRE_LINK_CATEGORY)
            cat, a, b = _seed_linked_categorised_pair(conn)
            # Link-shaped state: the category moved aside on leg a, none saved on leg b.
            conn.execute(
                text(
                    "UPDATE transactions SET category_id = NULL, pre_link_category_id = :c "
                    "WHERE id = :i"
                ),
                {"c": cat, "i": a},
            )
            conn.execute(text("UPDATE transactions SET category_id = NULL WHERE id = :i"), {"i": b})

        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), pre)
        with eng.connect() as conn:
            assert "pre_link_category_id" not in {
                c["name"] for c in inspect(conn).get_columns("transactions")
            }
            folded = dict(conn.execute(text("SELECT id, category_id FROM transactions")).all())
        assert folded == {a: cat, b: None}

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
        with eng.connect() as conn:
            saved = conn.execute(
                text("SELECT count(*) FROM transactions WHERE pre_link_category_id IS NOT NULL")
            ).scalar_one()
        assert saved == 0
    finally:
        eng.dispose()


# Written once; the pre-state comes from the script directory.
_REV_ASSET_CLASS_CURRENCY = "0045_asset_class_currency"
_ASSET_CLASS_CHECKS = {"ck_instruments_usd_only_asset_class", "ck_instruments_inr_only_asset_class"}
_INSERT_INSTRUMENT = (
    "INSERT INTO instruments (user_id, symbol, name, asset_class, currency, exchange) "
    "VALUES (:u, :s, :n, :a, :c, :e)"
)


@pytest.mark.slow
def test_asset_class_currency_migration_adds_the_checks_and_keeps_rows() -> None:
    """The CHECKs land, the three seeded instruments (and the FK into them) survive the batch
    rebuild, a mismatch is refused afterwards, and the downgrade drops the CHECKs but keeps the
    partial index's predicate (the only gate on the downgrade's re-create)."""
    eng = _migration_engine()
    try:
        v1_val = get_settings().v1_user_id.hex
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _down_revision_of(_REV_ASSET_CLASS_CURRENCY))
            for sym, name, cls, cur, exch in (
                ("AAPL", "Apple", "us_equity", "USD", "NASDAQ"),
                ("INF1", "Fund", "indian_mf", "INR", "MFCentral"),
                ("FCNR", "FCNR deposit", "fd", "USD", "OTHER"),
            ):
                conn.execute(
                    text(_INSERT_INSTRUMENT),
                    {"u": v1_val, "s": sym, "n": name, "a": cls, "c": cur, "e": exch},
                )
            aapl = conn.execute(text("SELECT id FROM instruments WHERE symbol = 'AAPL'")).scalar()
            conn.execute(
                text(
                    "INSERT INTO investment_transactions "
                    "(user_id, instrument_id, date, transaction_type, units, "
                    " amount_native_paise, fees_native_paise, fx_rate_to_inr, fingerprint) "
                    "VALUES (:u, :i, '2025-01-15', 'buy', 1000000000, 100000, 0, 1000000, :f)"
                ),
                {"u": v1_val, "i": aapl, "f": "f" * 64},
            )

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _REV_ASSET_CLASS_CURRENCY)
        with eng.connect() as conn:
            names = {c["name"] for c in inspect(conn).get_check_constraints("instruments")}
            assert names >= _ASSET_CLASS_CHECKS
            assert conn.execute(text("SELECT count(*) FROM instruments")).scalar_one() == 3
            assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
        with pytest.raises(IntegrityError), eng.begin() as conn:
            conn.execute(
                text(_INSERT_INSTRUMENT),
                {"u": v1_val, "s": "X", "n": "X", "a": "indian_equity", "c": "USD", "e": "NSE"},
            )

        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), _down_revision_of(_REV_ASSET_CLASS_CURRENCY))
        with eng.connect() as conn:
            names = {c["name"] for c in inspect(conn).get_check_constraints("instruments")}
            assert not _ASSET_CLASS_CHECKS & names
            assert conn.execute(text("SELECT count(*) FROM instruments")).scalar_one() == 3
            index_sql = conn.execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE name = 'uq_instruments_active_user_symbol_currency'"
                )
            ).scalar_one_or_none()
        assert index_sql is not None and "archived_at IS NULL" in index_sql

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
    finally:
        eng.dispose()


@pytest.mark.slow
def test_asset_class_currency_migration_refuses_a_mismatched_row() -> None:
    """A row the CHECK would refuse stops the upgrade at the count-only preflight: the message
    says how many and how to fix it, and names no symbol."""
    eng = _migration_engine()
    try:
        v1_val = get_settings().v1_user_id.hex
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _down_revision_of(_REV_ASSET_CLASS_CURRENCY))
            conn.execute(
                text(_INSERT_INSTRUMENT),
                {
                    "u": v1_val,
                    "s": "SECRETSYM",
                    "n": "Secret",
                    "a": "indian_equity",
                    "c": "USD",
                    "e": "NSE",
                },
            )
        with pytest.raises(RuntimeError) as exc, eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _REV_ASSET_CLASS_CURRENCY)
        assert "1 instrument row(s) of an INR-only asset class" in str(exc.value)
        assert "fix in SQL" in str(exc.value)
        assert "SECRETSYM" not in str(exc.value)
    finally:
        eng.dispose()


_REV_ROOT_COLOR = "0046_root_category_color"
_INSERT_CATEGORY = (
    "INSERT INTO categories (user_id, name, kind, is_seeded, color, parent_id) "
    "VALUES (:u, :n, 'spend', 0, :c, :p)"
)


@pytest.mark.slow
def test_root_color_migration_adds_the_check_and_keeps_rows() -> None:
    """The CHECK lands, a root, its inheriting child and the FK into the child survive the batch
    rebuild, a colourless root is refused afterwards, and the downgrade drops the CHECK but keeps
    the partial index's predicate (the only gate on the downgrade's re-create)."""
    eng = _migration_engine()
    try:
        v1_val = get_settings().v1_user_id.hex
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _down_revision_of(_REV_ROOT_COLOR))
            conn.execute(
                text(_INSERT_CATEGORY), {"u": v1_val, "n": "ProbeRoot", "c": "#2a78d6", "p": None}
            )
            root = conn.execute(
                text("SELECT id FROM categories WHERE name = 'ProbeRoot'")
            ).scalar_one()
            conn.execute(
                text(_INSERT_CATEGORY), {"u": v1_val, "n": "ProbeChild", "c": None, "p": root}
            )
            child = conn.execute(
                text("SELECT id FROM categories WHERE name = 'ProbeChild'")
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO merchant_tag_map (user_id, merchant_normalized, category_id) "
                    "VALUES (:u, 'probemerchant', :c)"
                ),
                {"u": v1_val, "c": child},
            )
            before = conn.execute(text("SELECT count(*) FROM categories")).scalar_one()

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _REV_ROOT_COLOR)
        with eng.connect() as conn:
            names = {c["name"] for c in inspect(conn).get_check_constraints("categories")}
            assert "ck_categories_root_has_color" in names
            assert conn.execute(text("SELECT count(*) FROM categories")).scalar_one() == before
            assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
        with pytest.raises(IntegrityError), eng.begin() as conn:
            conn.execute(
                text(_INSERT_CATEGORY), {"u": v1_val, "n": "Colourless", "c": None, "p": None}
            )

        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), _down_revision_of(_REV_ROOT_COLOR))
        with eng.connect() as conn:
            names = {c["name"] for c in inspect(conn).get_check_constraints("categories")}
            assert "ck_categories_root_has_color" not in names
            assert conn.execute(text("SELECT count(*) FROM categories")).scalar_one() == before
            index_sql = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE name = 'uq_categories_active_user_name'")
            ).scalar_one_or_none()
        assert index_sql is not None and "archived_at IS NULL" in index_sql

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
    finally:
        eng.dispose()


@pytest.mark.slow
def test_root_color_migration_refuses_a_colourless_root() -> None:
    """A colourless root stops the upgrade at the count-only preflight: the message says how many
    and how to fix it, and names no category."""
    eng = _migration_engine()
    try:
        v1_val = get_settings().v1_user_id.hex
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _down_revision_of(_REV_ROOT_COLOR))
            conn.execute(
                text(_INSERT_CATEGORY), {"u": v1_val, "n": "SECRETCAT", "c": None, "p": None}
            )
        with pytest.raises(RuntimeError) as exc, eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _REV_ROOT_COLOR)
        assert "1 root category row(s) have no color" in str(exc.value)
        assert "fix in SQL" in str(exc.value)
        assert "SECRETCAT" not in str(exc.value)
    finally:
        eng.dispose()


# Written once; the pre-state comes from the script directory.
_REV_RETIRE_CARD_BILL = "0047_retire_card_bill_category"
_CARD_BILL = "Credit Card Payments"
_INSERT_CARD_BILL_CATEGORY = (
    "INSERT INTO categories (user_id, name, kind, is_seeded, color, parent_id) "
    "VALUES (:u, :n, 'spend', :s, :c, :p)"
)


def _category_id(conn: Connection, user: str, name: str) -> int:
    return conn.execute(
        text("SELECT id FROM categories WHERE user_id = :u AND name = :n AND archived_at IS NULL"),
        {"u": user, "n": name},
    ).scalar_one()


def _archived_at(conn: Connection, category_id: int) -> object:
    return conn.execute(
        text("SELECT archived_at FROM categories WHERE id = :i"), {"i": category_id}
    ).scalar_one()


@pytest.mark.slow
def test_card_bill_category_migration_archives_the_seed_and_keeps_rows() -> None:
    """ADR-0002 rule 7b: the seeded spend "Credit Card Payments" is archived for every user, rows
    and tag rules pointing at it keep it, a user-made root of the same name is left alone, and
    the downgrade un-archives the seed."""
    eng = _migration_engine()
    try:
        v1_val = get_settings().v1_user_id.hex
        other_user = uuid.uuid4().hex
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _down_revision_of(_REV_RETIRE_CARD_BILL))
            seed = _category_id(conn, v1_val, _CARD_BILL)
            assert _archived_at(conn, seed) is None
            conn.execute(
                text("INSERT INTO accounts (user_id, name, type) VALUES (:u, 'HDFC', 'bank')"),
                {"u": v1_val},
            )
            acct = conn.execute(text("SELECT id FROM accounts")).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO transactions "
                    "(user_id, account_id, date, amount_paise, transaction_type, "
                    " merchant_normalized, fingerprint, source, category_id) "
                    "VALUES (:u, :a, '2026-03-10', -50000, 'spend', 'cc autopay', 'fp-bill', "
                    " 'import', :c)"
                ),
                {"u": v1_val, "a": acct, "c": seed},
            )
            conn.execute(
                text(
                    "INSERT INTO merchant_tag_map (user_id, merchant_normalized, category_id) "
                    "VALUES (:u, 'cc autopay', :c)"
                ),
                {"u": v1_val, "c": seed},
            )
            conn.execute(text("INSERT INTO users (id) VALUES (:u)"), {"u": other_user})
            conn.execute(
                text(_INSERT_CARD_BILL_CATEGORY),
                {"u": other_user, "n": _CARD_BILL, "s": 0, "c": "#2a78d6", "p": None},
            )
            mine = _category_id(conn, other_user, _CARD_BILL)

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _REV_RETIRE_CARD_BILL)
        with eng.connect() as conn:
            assert _archived_at(conn, seed) is not None
            txn = conn.execute(text("SELECT category_id, transaction_type FROM transactions")).one()
            assert tuple(txn) == (seed, "spend")
            assert (
                conn.execute(
                    text(
                        "SELECT category_id FROM merchant_tag_map "
                        "WHERE merchant_normalized = 'cc autopay'"
                    )
                ).scalar_one()
                == seed
            )
            assert _archived_at(conn, mine) is None

        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), _down_revision_of(_REV_RETIRE_CARD_BILL))
        with eng.connect() as conn:
            assert _archived_at(conn, seed) is None

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
    finally:
        eng.dispose()


@pytest.mark.slow
def test_card_bill_category_migration_archives_a_promoted_seed_with_its_children() -> None:
    """A user who promoted the seed to a root keeps its children under it (ADR-0012 rule 9), so
    archiving the root archives them too, with the same stamp."""
    eng = _migration_engine()
    try:
        promoted_user = uuid.uuid4().hex
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _down_revision_of(_REV_RETIRE_CARD_BILL))
            conn.execute(text("INSERT INTO users (id) VALUES (:u)"), {"u": promoted_user})
            conn.execute(
                text(_INSERT_CARD_BILL_CATEGORY),
                {"u": promoted_user, "n": _CARD_BILL, "s": 1, "c": "#c23b6b", "p": None},
            )
            root = _category_id(conn, promoted_user, _CARD_BILL)
            conn.execute(
                text(_INSERT_CARD_BILL_CATEGORY),
                {"u": promoted_user, "n": "Card Fees", "s": 1, "c": None, "p": root},
            )
            child = _category_id(conn, promoted_user, "Card Fees")

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _REV_RETIRE_CARD_BILL)
        with eng.connect() as conn:
            stamp = _archived_at(conn, root)
            assert stamp is not None
            assert _archived_at(conn, child) == stamp

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
    finally:
        eng.dispose()


_REV_STREAMLINE_CATEGORIES = "0049_streamline_category_taxonomy"
_REV_INVESTMENT_ACCOUNTS = "0050_investment_accounts_and_attribution"
_REV_SPEND_SEAM = "0051_spend_seam_and_investment_transfer"
_REV_MARKET_PRICING = "0052_market_pricing"
_REV_PORTFOLIO_DAILY_SNAPSHOTS = "0053_portfolio_daily_snapshots"


@pytest.mark.slow
def test_0049_upgrade_reassigns_utility_bills_to_bills_and_utilities() -> None:
    """Historical utility transactions must be reassigned to the new subcategory
    'Bills & Utilities' under 'Housing & Utilities', not 'Rent & Maintenance'."""
    eng = _migration_engine()
    try:
        user_id = uuid.uuid4().hex
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _down_revision_of(_REV_STREAMLINE_CATEGORIES))
            conn.execute(text("INSERT INTO users (id) VALUES (:u)"), {"u": user_id})
            conn.execute(
                text(
                    "INSERT INTO accounts (user_id, name, type) VALUES (:u, 'Primary Bank', 'bank')"
                ),
                {"u": user_id},
            )
            acct_id = conn.execute(
                text("SELECT id FROM accounts WHERE user_id = :u"), {"u": user_id}
            ).scalar_one()

            conn.execute(
                text(
                    "INSERT INTO categories (user_id, name, kind, is_seeded, color, parent_id) "
                    "VALUES (:u, 'Bills & Utilities', 'spend', 1, '#6c5cd6', NULL)"
                ),
                {"u": user_id},
            )
            root_bu_id = conn.execute(
                text(
                    "SELECT id FROM categories WHERE user_id = :u AND name = 'Bills & Utilities' "
                    "AND parent_id IS NULL"
                ),
                {"u": user_id},
            ).scalar_one()

            conn.execute(
                text(
                    "INSERT INTO categories (user_id, name, kind, is_seeded, color, parent_id) "
                    "VALUES (:u, 'Electricity', 'spend', 1, NULL, :p)"
                ),
                {"u": user_id, "p": root_bu_id},
            )
            elec_id = conn.execute(
                text("SELECT id FROM categories WHERE user_id = :u AND name = 'Electricity'"),
                {"u": user_id},
            ).scalar_one()

            conn.execute(
                text(
                    "INSERT INTO categories (user_id, name, kind, is_seeded, color, parent_id) "
                    "VALUES (:u, 'Household & Living', 'spend', 1, '#6c5cd6', NULL)"
                ),
                {"u": user_id},
            )

            conn.execute(
                text(
                    "INSERT INTO transactions (user_id, account_id, category_id, amount_paise, "
                    "transaction_type, date, fingerprint, source, merchant_normalized) "
                    "VALUES (:u, :a, :c, 150000, 'spend', '2026-01-01', 'fp-elec-1', 'manual', "
                    "'bescom electricity')"
                ),
                {"u": user_id, "a": acct_id, "c": elec_id},
            )

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _REV_STREAMLINE_CATEGORIES)

        with eng.connect() as conn:
            housing_id = conn.execute(
                text(
                    "SELECT id FROM categories WHERE user_id = :u AND name = 'Housing & Utilities' "
                    "AND parent_id IS NULL AND archived_at IS NULL"
                ),
                {"u": user_id},
            ).scalar_one()

            sub_bu_id = conn.execute(
                text(
                    "SELECT id FROM categories WHERE user_id = :u AND name = 'Bills & Utilities' "
                    "AND parent_id = :p AND archived_at IS NULL"
                ),
                {"u": user_id, "p": housing_id},
            ).scalar_one()

            assigned_cat = conn.execute(
                text("SELECT category_id FROM transactions WHERE user_id = :u"),
                {"u": user_id},
            ).scalar_one()
            assert assigned_cat == sub_bu_id
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0049_downgrade_reverses_category_streamline() -> None:
    """0049 downgrade must restore 0048 root names/colors and un-archive retired categories."""
    eng = _migration_engine()
    try:
        user_id = uuid.uuid4().hex
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _REV_STREAMLINE_CATEGORIES)
            conn.execute(text("INSERT INTO users (id) VALUES (:u)"), {"u": user_id})
            conn.execute(
                text(
                    "INSERT INTO categories (user_id, name, kind, is_seeded, color, parent_id) "
                    "VALUES (:u, 'Housing & Utilities', 'spend', 1, '#6c5cd6', NULL)"
                ),
                {"u": user_id},
            )
            housing_id = conn.execute(
                text(
                    "SELECT id FROM categories WHERE user_id = :u AND name = 'Housing & Utilities'"
                ),
                {"u": user_id},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO categories (user_id, name, kind, is_seeded, color, parent_id) "
                    "VALUES (:u, 'Bills & Utilities', 'spend', 1, NULL, :p)"
                ),
                {"u": user_id, "p": housing_id},
            )

        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), _down_revision_of(_REV_STREAMLINE_CATEGORIES))

        with eng.connect() as conn:
            reverted = conn.execute(
                text("SELECT name FROM categories WHERE user_id = :u AND id = :id"),
                {"u": user_id, "id": housing_id},
            ).scalar_one()
            assert reverted == "Household & Living"

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0049_upgrade_with_archived_household_and_living() -> None:
    """Migration 0049 must not fail if user previously archived Household & Living."""
    eng = _migration_engine()
    try:
        user_id = uuid.uuid4().hex
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _down_revision_of(_REV_STREAMLINE_CATEGORIES))
            conn.execute(text("INSERT INTO users (id) VALUES (:u)"), {"u": user_id})
            conn.execute(
                text(
                    "INSERT INTO categories (user_id, name, kind, is_seeded, color, parent_id, "
                    "archived_at) VALUES (:u, 'Household & Living', 'spend', 1, '#6c5cd6', NULL, "
                    "'2026-01-01 00:00:00')"
                ),
                {"u": user_id},
            )

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _REV_STREAMLINE_CATEGORIES)

        with eng.connect() as conn:
            hc = conn.execute(
                text(
                    "SELECT id FROM categories WHERE user_id = :u AND name = 'Healthcare' "
                    "AND parent_id IS NULL AND archived_at IS NULL"
                ),
                {"u": user_id},
            ).scalar_one_or_none()
            assert hc is not None
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0049_seeds_missing_merchant_aliases_and_tag_maps() -> None:
    """Existing users should receive newly added merchant aliases and seed tag maps."""
    eng = _migration_engine()
    try:
        user_id = uuid.uuid4().hex
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _down_revision_of(_REV_STREAMLINE_CATEGORIES))
            conn.execute(text("INSERT INTO users (id) VALUES (:u)"), {"u": user_id})

        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _REV_STREAMLINE_CATEGORIES)

        with eng.connect() as conn:
            alias_canonical = conn.execute(
                text(
                    "SELECT canonical FROM merchant_alias WHERE user_id = :u AND pattern = 'irctc'"
                ),
                {"u": user_id},
            ).scalar_one_or_none()
            assert alias_canonical == "irctc"

            tag_cat = conn.execute(
                text(
                    "SELECT c.name FROM merchant_tag_map m "
                    "JOIN categories c ON c.id = m.category_id "
                    "WHERE m.user_id = :u AND m.merchant_normalized = 'irctc'"
                ),
                {"u": user_id},
            ).scalar_one_or_none()
            assert tag_cat == "Flights & Trains"
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0050_upgrade_backfills_and_links_default_accounts() -> None:
    """Migration 0050 must create Default Portfolio and backfill account_id onto historical rows."""
    eng = _migration_engine()
    try:
        user_id = uuid.uuid4().hex
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _down_revision_of(_REV_INVESTMENT_ACCOUNTS))
            conn.execute(text("INSERT INTO users (id) VALUES (:u)"), {"u": user_id})
            inst_id = conn.execute(
                text(
                    "INSERT INTO instruments "
                    "(user_id, symbol, name, asset_class, currency, exchange) "
                    "VALUES (:u, 'HDFC', 'HDFC Bank', 'indian_equity', 'INR', 'NSE')"
                ),
                {"u": user_id},
            ).lastrowid
            txn_id = conn.execute(
                text(
                    "INSERT INTO investment_transactions (user_id, instrument_id, date, "
                    "transaction_type, units, price_per_unit_native, amount_native_paise, "
                    "fx_rate_to_inr, fingerprint) "
                    "VALUES (:u, :i, '2026-01-15', 'buy', 1000000000, 150000, 1500000, "
                    "1000000, 'test_fp_0050')"
                ),
                {"u": user_id, "i": inst_id},
            ).lastrowid

        # Upgrade to 0050
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _REV_INVESTMENT_ACCOUNTS)

        with eng.connect() as conn:
            acc_row = (
                conn.execute(
                    text(
                        "SELECT id, name, provider, external_account_ref, currency, is_active "
                        "FROM investment_accounts WHERE user_id = :u"
                    ),
                    {"u": user_id},
                )
                .mappings()
                .one()
            )
            assert acc_row["name"] == "Default Portfolio"
            assert acc_row["provider"] == "manual"
            assert acc_row["external_account_ref"] == "default"
            assert acc_row["currency"] == "INR"
            assert acc_row["is_active"] == 1

            linked_acc_id = conn.execute(
                text("SELECT account_id FROM investment_transactions WHERE id = :t"),
                {"t": txn_id},
            ).scalar_one()
            assert linked_acc_id == acc_row["id"]

        # Downgrade back to 0049
        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), _down_revision_of(_REV_INVESTMENT_ACCOUNTS))

        with eng.connect() as conn:
            tbl_exists = conn.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='investment_accounts'"
                )
            ).scalar_one_or_none()
            assert tbl_exists is None

        # Re-upgrade to head
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0051_upgrade_adds_linked_spend_and_system_category() -> None:
    """Migration 0051 must add linked_spend_txn_id and seed system:investment_transfer category."""
    eng = _migration_engine()
    try:
        user_id = uuid.uuid4().hex
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _down_revision_of(_REV_SPEND_SEAM))
            conn.execute(text("INSERT INTO users (id) VALUES (:u)"), {"u": user_id})

        # Upgrade to 0051
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _REV_SPEND_SEAM)

        with eng.connect() as conn:
            # Check system:investment_transfer category was seeded
            cat_row = (
                conn.execute(
                    text(
                        "SELECT id, name, kind, is_seeded, color, parent_id "
                        "FROM categories WHERE user_id = :u AND name = 'system:investment_transfer'"
                    ),
                    {"u": user_id},
                )
                .mappings()
                .one()
            )
            assert cat_row["name"] == "system:investment_transfer"
            assert cat_row["kind"] == "spend"
            assert cat_row["is_seeded"] == 1
            assert cat_row["color"] == "#64748b"
            assert cat_row["parent_id"] is None

            # Check linked_spend_txn_id column exists
            cols = [c["name"] for c in inspect(conn).get_columns("investment_transactions")]
            assert "linked_spend_txn_id" in cols

        # Downgrade back to 0050
        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), _down_revision_of(_REV_SPEND_SEAM))

        with eng.connect() as conn:
            cols = [c["name"] for c in inspect(conn).get_columns("investment_transactions")]
            assert "linked_spend_txn_id" not in cols

            cat_exists = conn.execute(
                text(
                    "SELECT id FROM categories "
                    "WHERE user_id = :u AND name = 'system:investment_transfer'"
                ),
                {"u": user_id},
            ).scalar_one_or_none()
            assert cat_exists is None

        # Re-upgrade to head
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0052_upgrade_creates_market_pricing_tables() -> None:
    """Migration 0052 must create market pricing tables with indexes and constraints."""
    eng = _migration_engine()
    tables = [
        "market_instruments",
        "market_price_history",
        "market_price_coverage",
        "market_corporate_actions",
        "market_instrument_pins",
        "market_price_jobs",
        "market_host_state",
        "market_task_runs",
    ]
    try:
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _down_revision_of(_REV_MARKET_PRICING))

        with eng.connect() as conn:
            insp = inspect(conn)
            for tbl in tables:
                assert not insp.has_table(tbl)

        # Upgrade to 0052
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _REV_MARKET_PRICING)

        with eng.connect() as conn:
            insp = inspect(conn)
            for tbl in tables:
                assert insp.has_table(tbl)

            # Check market_price_history columns
            col_names = {c["name"] for c in insp.get_columns("market_price_history")}
            assert col_names == {"instrument_id", "nav_date", "nav_micros"}

            # Check market_price_jobs indexes
            job_indexes = {i["name"] for i in insp.get_indexes("market_price_jobs")}
            assert "uq_market_price_jobs_active" in job_indexes
            assert "ix_market_price_jobs_claim" in job_indexes

        # Downgrade back to 0051
        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), _down_revision_of(_REV_MARKET_PRICING))

        with eng.connect() as conn:
            insp = inspect(conn)
            for tbl in tables:
                assert not insp.has_table(tbl)

        # Re-upgrade to head
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
    finally:
        eng.dispose()


@pytest.mark.slow
def test_0053_upgrade_creates_portfolio_daily_snapshots() -> None:
    """Migration 0053 must create portfolio_daily_snapshots with composite PK and constraints."""
    eng = _migration_engine()
    tbl = "portfolio_daily_snapshots"
    try:
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _down_revision_of(_REV_PORTFOLIO_DAILY_SNAPSHOTS))

        with eng.connect() as conn:
            insp = inspect(conn)
            assert not insp.has_table(tbl)

        # Upgrade to 0053
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), _REV_PORTFOLIO_DAILY_SNAPSHOTS)

        with eng.connect() as conn:
            insp = inspect(conn)
            assert insp.has_table(tbl)

            # Check columns
            col_names = {c["name"] for c in insp.get_columns(tbl)}
            assert col_names == {
                "user_id",
                "scope_type",
                "scope_id",
                "snapshot_date",
                "portfolio_value_paise",
                "invested_capital_paise",
                "net_cashflow_paise",
                "twr_index_micros",
                "benchmark_value_paise",
                "created_at",
            }

            # Check primary key columns
            pk_cols = insp.get_pk_constraint(tbl)["constrained_columns"]
            assert tuple(pk_cols) == ("user_id", "scope_type", "scope_id", "snapshot_date")

        # Downgrade back to 0052
        with eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), _down_revision_of(_REV_PORTFOLIO_DAILY_SNAPSHOTS))

        with eng.connect() as conn:
            insp = inspect(conn)
            assert not insp.has_table(tbl)

        # Re-upgrade to head
        with eng.begin() as conn:
            command.upgrade(_alembic_cfg(conn), "head")
    finally:
        eng.dispose()
