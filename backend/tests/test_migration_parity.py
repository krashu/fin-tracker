"""Schema parity: ``alembic upgrade head`` must match ``Base.metadata.create_all``.

Runs on every pytest invocation (~150 ms). Catches the bug where a model
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
from sqlalchemy import Engine, create_engine, event, inspect, select, text
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
            "uqs": sorted(tuple(u["column_names"]) for u in insp.get_unique_constraints(t)),
            "ixs": sorted(
                (i["name"], tuple(i["column_names"]), bool(i["unique"]))
                for i in insp.get_indexes(t)
            ),
            "fks": sorted(
                (
                    tuple(f["constrained_columns"]),
                    f["referred_table"],
                    tuple(f["referred_columns"]),
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
_ARCHIVED_FLAT_SEEDS = frozenset({"Income", "Transfer"})


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

            # 9 spend parents (Food & Dining, Household & Living, Bills & Utilities,
            # Commute & Transportation, Shopping & Lifestyle, Family & Social,
            # Savings & Investments, Loans & Settlements, Other) + 1 income parent = 10,
            # holding 55 children total (49 spend + 6 income) — see provisioning.py's
            # _DEFAULT_SPEND_TAXONOMY / _DEFAULT_INCOME_TAXONOMY, the taxonomy's one source
            # of truth (ADR-0012).
            assert len(parents) == 10
            assert len(children) == 55
            assert all(c.is_seeded is True for c in cats)
            assert all(c.created_at is not None for c in cats)

            # A per-parent child-NAME set, not just a count — a same-count swap between two
            # siblings (e.g. a typo trading "Coffee & Tea" for something else) would still
            # pass len(children) == 55.
            food_parent = next(
                c for c in parents if c.kind == "spend" and c.name == "Food & Dining"
            )
            food_children = {c.name for c in children if c.parent_id == food_parent.id}
            assert food_children == {
                "Food",
                "Groceries",
                "Online Food Delivery",
                "Restaurants & Cafes",
                "Quick Bites & Snacks",
                "Coffee & Tea",
            }

            # Vestigial flat seeds: still spend-kind, now archived.
            archived = [c for c in cats if c.archived_at is not None]
            assert {c.name for c in archived} == _ARCHIVED_FLAT_SEEDS
            assert all(c.kind == "spend" for c in archived)
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
                        user_id=get_settings().v1_user_id,
                        name="UserMade",
                        is_seeded=False,
                    ),
                    Category(
                        user_id=get_settings().v1_user_id,
                        name="ArchivedUserMade",
                        is_seeded=False,
                        archived_at=now,
                    ),
                    Category(
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
            fk = next(
                tuple(f["constrained_columns"])
                for f in insp.get_foreign_keys("investment_transactions")
                if len(f["constrained_columns"]) == 2
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
            # The second of two genuinely-distinct identical transactions.
            conn.execute(
                text(
                    "INSERT INTO investment_transactions "
                    "(user_id, instrument_id, date, transaction_type, units, "
                    " amount_native_paise, fees_native_paise, fx_rate_to_inr, "
                    " fingerprint, occurrence) "
                    "VALUES (:u, :i, '2025-01-15', 'buy', 1000000000, 100000, 0, 1000000, "
                    " 'dupe', 1)"
                ).bindparams(u=uid_hex, i=inst_id)
            )

        with pytest.raises(IntegrityError), eng.begin() as conn:
            command.downgrade(_alembic_cfg(conn), "0026_rename_switch_pair_id_to_pair_id")
    finally:
        eng.dispose()


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

    command.upgrade(_cli_cfg(), "head")

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
