"""Shared helpers for tests that must see every owned table (ADR-0003 rule 11).

The owned set is derived from ``Base.metadata``, so a new table is covered the moment it is
declared. Used by the teardown test and the account-deletion test.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Table, func, select, update
from sqlalchemy.orm import Session

from app.core import clock
from app.models import (
    Base,
    ImportBatch,
    PeerContact,
    PeerEntry,
    PortfolioDailySnapshot,
    Transaction,
)

# ADR-0003 & ADR-0021: the only tables without an owner. ``users`` is the tenant root itself.
GLOBAL_TABLES = frozenset(
    {
        "benchmarks",
        "benchmark_nav",
        "fx_rates",
        "market_instruments",
        "market_price_history",
        "market_price_coverage",
        "market_corporate_actions",
        "market_instrument_pins",
        "market_price_jobs",
        "market_host_state",
        "market_task_runs",
    }
)
TENANT_ROOT = "users"


def owned_tables() -> list[Table]:
    return [
        t
        for t in Base.metadata.sorted_tables
        if t.name not in GLOBAL_TABLES and t.name != TENANT_ROOT
    ]


def rows_owned_by(session: Session, table: Table, user_id: UUID) -> int:
    return (
        session.scalar(select(func.count()).select_from(table).where(table.c.user_id == user_id))
        or 0
    )


def seed_rows_demo_seed_lacks(session: Session, user_id: UUID) -> None:
    """Add what ``seed_demo_data`` does not produce, so every owned table holds a row. Flushes only.

    The demo seed has no import batch: link one transaction to it, so teardown must also get the
    transactions -> import_batches order right under FK enforcement. A refund -> original link
    (ADR-0009 rule 13) is a NO ACTION self-FK that a single per-user DELETE must not trip.
    """
    batch = ImportBatch(
        user_id=user_id, source_file_hash="0" * 64, parser_name="hdfc_cc", status="completed"
    )
    session.add(batch)
    session.flush()
    first_txn = session.scalar(
        select(Transaction.id).where(Transaction.user_id == user_id).limit(1)
    )
    session.execute(
        update(Transaction)
        .where(Transaction.id == first_txn, Transaction.user_id == user_id)
        .values(import_batch_id=batch.id)
    )
    refund, original = session.scalars(
        select(Transaction.id)
        .where(Transaction.user_id == user_id, Transaction.id != first_txn)
        .limit(2)
    ).all()
    session.execute(
        update(Transaction)
        .where(Transaction.id == refund, Transaction.user_id == user_id)
        .values(refund_of_id=original)
    )
    contact = PeerContact(user_id=user_id, name="Test Contact")
    session.add(contact)
    session.flush()
    entry = PeerEntry(
        user_id=user_id,
        contact_id=contact.id,
        transaction_id=first_txn,
        date=clock.today(),
        amount_paise=1000,
        direction="you_gave",
    )
    session.add(entry)
    session.flush()
    snapshot = PortfolioDailySnapshot(
        user_id=user_id,
        scope_type="all",
        scope_id=0,
        snapshot_date=clock.today(),
        portfolio_value_paise=100000,
        invested_capital_paise=100000,
        net_cashflow_paise=100000,
        twr_index_micros=1000000,
    )
    session.add(snapshot)
    session.flush()
