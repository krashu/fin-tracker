"""Direct-DB demo seeder — populates a fresh DB with the demo dataset on boot.

The app lifespan calls :func:`seed_demo_data` after migrations bring a fresh DB
to ``head`` (see :mod:`app.main`). Unlike :mod:`scripts.seed_dev_data` — which
drives the *running* HTTP API and so can't run before the server is listening —
this writes straight to the ORM in-process, on the same engine, so a plain
``make backend`` comes up with a populated app and no separate seed step.

Dataset lives in :mod:`app.core.demo_data` (shared with the HTTP script so the
two never drift). The load-bearing invariants — merchant normalization, the
PRD §F4 fingerprint, and F3 tag learning — are reused verbatim from their
existing single-source-of-truth functions (``normalize_merchant``,
``transaction_fingerprint``, ``record_tag``); re-implementing them here would let
the fingerprint inputs drift from the POST route and silently break dedup.

Only ever invoked on an empty DB (the lifespan gates on zero accounts /
transactions / investment-txns), so there are no fingerprint conflicts to handle
and a single commit is enough. Network is deliberately untouched — the benchmark
NAV backfill (mfapi) stays in the HTTP script; blocking boot on an external fetch
is not worth it, and benchmark history isn't needed for the core demo.
"""

from __future__ import annotations

from datetime import date as date_t
from decimal import Decimal
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import clock
from app.core.demo_data import BANK_INCOME, CARD_REFUNDS, CARD_SPENDS, INSTRUMENTS
from app.core.log_config import get_logger
from app.models import Account, Category, Instrument, InvestmentTransaction, Transaction
from app.services.fingerprint import transaction_fingerprint
from app.services.merchant import normalize_merchant
from app.services.merchant_labels import learn_merchant_memory
from app.services.nav_snapshot_service import as_valuation_stamp
from app.services.transaction_labels import resolve_label_names, set_labels_on_transaction

logger = get_logger(__name__)


class DemoSeedCounts(NamedTuple):
    """What the seed inserted — logged by the lifespan for visibility."""

    accounts: int
    transactions: int
    instruments: int
    investment_transactions: int


def _get_or_create_account(
    session: Session, *, user_id: UUID, name: str, **fields: object
) -> Account:
    """Return the active account named ``name`` for ``user_id``, creating it if absent.

    Flushes on create so the autoincrement id is available for the txn
    fingerprint + FK. Find-or-create keeps the seeder safe even though the
    lifespan only calls it on an empty DB.
    """
    existing = session.scalar(
        select(Account).where(
            Account.user_id == user_id,
            Account.name == name,
            Account.archived_at.is_(None),
        )
    )
    if existing is not None:
        return existing
    account = Account(user_id=user_id, name=name, **fields)
    session.add(account)
    session.flush()
    return account


def _categories_by_kind(
    session: Session, *, user_id: UUID
) -> tuple[dict[str, int], dict[str, int]]:
    """(spend_by_name, income_by_name) — lowercase name → id, active only.

    Migrations 0003/0012 seed both sets (incl. "Other") for the demo/v1 user, and
    the caller only runs on a fresh DB right after migrations, so both maps are
    populated. Raising below if "Other" is missing turns a category-drift bug into
    a loud failure rather than a null-category write.
    """
    cats = session.scalars(
        select(Category).where(Category.user_id == user_id, Category.archived_at.is_(None))
    ).all()
    spend = {c.name.lower(): c.id for c in cats if c.kind == "spend"}
    income = {c.name.lower(): c.id for c in cats if c.kind == "income"}
    if "other" not in spend or "other" not in income:
        raise RuntimeError(
            "demo seed: default 'Other' category missing — expected migrations "
            "0003/0012 to seed it for the demo user before seeding runs."
        )
    return spend, income


def _add_transaction(
    session: Session,
    *,
    user_id: UUID,
    account_id: int,
    on: date_t,
    amount_paise: int,
    transaction_type: str,
    merchant_raw: str,
    category_id: int,
    labels: tuple[str, ...] = (),
) -> None:
    """Build + add one confirmed manual transaction, mirroring the POST route.

    Resolves labels and learns the merchant → category/label memory (spend/refund
    only, via :func:`learn_merchant_memory`) BEFORE adding the row — same slot and
    ordering as ``create_transaction``, so the seeder exercises the same path and
    the demo user's ``merchant_tag_map`` / ``merchant_label_map`` are populated for
    a later import to prefill. Labels are linked after a flush assigns the txn id.
    Caller owns the commit (the empty-DB single-commit seeder has no 409 handler,
    but a fresh DB has no fingerprint conflicts).
    """
    merchant_normalized = normalize_merchant(merchant_raw) if merchant_raw else ""
    resolved = resolve_label_names(session, user_id=user_id, names=labels) if labels else []
    learn_merchant_memory(
        session,
        user_id=user_id,
        merchant_normalized=merchant_normalized,
        transaction_type=transaction_type,
        category_id=category_id,
        label_ids=[label.id for label in resolved],
    )
    txn = Transaction(
        user_id=user_id,
        account_id=account_id,
        date=on,
        amount_paise=amount_paise,
        transaction_type=transaction_type,
        merchant_raw=merchant_raw,
        merchant_normalized=merchant_normalized,
        category_id=category_id,
        fingerprint=transaction_fingerprint(
            txn_date=on,
            amount_paise=amount_paise,
            normalized_merchant=merchant_normalized,
            account_id=account_id,
        ),
        source="manual",
        import_batch_id=None,
        confirmed_at=clock.utcnow(),
    )
    session.add(txn)
    if resolved:
        session.flush()  # assign txn.id before linking labels
        set_labels_on_transaction(session, txn=txn, labels=resolved)


def seed_demo_data(session: Session, *, user_id: UUID) -> DemoSeedCounts:
    """Seed accounts, spending, and investments for ``user_id`` in one commit.

    Idempotent for accounts/instruments (find-or-create); transactions are added
    unconditionally, which is safe because the lifespan only calls this on an
    empty DB. Returns the inserted counts for logging.
    """
    cc = _get_or_create_account(
        session,
        user_id=user_id,
        name="Axis Flipkart",
        type="credit_card",
        issuer="axis",
        # Placeholder last-4 — the demo account is a public "Try the demo" login.
        # NEVER use a real card's last-4; the demo DB carries only synthetic ids.
        last4="4321",
        currency="INR",
        opening_balance_paise=0,
    )
    bank = _get_or_create_account(
        session,
        user_id=user_id,
        name="HDFC Savings",
        type="bank",
        issuer="hdfc",
        currency="INR",
        opening_balance_paise=0,
    )

    spend_cats, income_cats = _categories_by_kind(session, user_id=user_id)
    other_spend, other_income = spend_cats["other"], income_cats["other"]
    txn_count = 0

    for row in CARD_SPENDS:
        _add_transaction(
            session,
            user_id=user_id,
            account_id=cc.id,
            on=date_t.fromisoformat(row.date),
            amount_paise=-row.rupees * 100,  # spend = negative
            transaction_type="spend",
            merchant_raw=row.merchant,
            category_id=spend_cats.get(row.category.lower(), other_spend),
            labels=row.labels,
        )
        txn_count += 1
    for row in CARD_REFUNDS:
        _add_transaction(
            session,
            user_id=user_id,
            account_id=cc.id,
            on=date_t.fromisoformat(row.date),
            amount_paise=row.rupees * 100,  # refund = positive, same category
            transaction_type="refund",
            merchant_raw=row.merchant,
            category_id=spend_cats.get(row.category.lower(), other_spend),
            labels=row.labels,
        )
        txn_count += 1
    for inc in BANK_INCOME:
        _add_transaction(
            session,
            user_id=user_id,
            account_id=bank.id,
            on=date_t.fromisoformat(inc.date),
            amount_paise=inc.rupees * 100,  # income = positive on the bank
            transaction_type="income",
            merchant_raw=inc.source,
            category_id=income_cats.get(inc.category.lower(), other_income),
            labels=inc.labels,
        )
        txn_count += 1

    inst_count, inv_txn_count = _seed_investments(session, user_id=user_id)

    session.commit()
    return DemoSeedCounts(
        accounts=2,
        transactions=txn_count,
        instruments=inst_count,
        investment_transactions=inv_txn_count,
    )


def _seed_investments(session: Session, *, user_id: UUID) -> tuple[int, int]:
    """Find-or-create each instrument + its txns. Returns (instruments, txns) made.

    Only seeds an instrument's txns when the instrument is newly created (manual
    investment txns have no dedup key), mirroring the HTTP script's gate. INR
    instruments stamp ``fx_rate_to_inr=1`` — the identity conversion (PRD §F7);
    ``fingerprint`` stays NULL (manual rows aren't CAS-deduped). ``nav_updated_at`` is
    the seed date, because the seeded NAV is a made-up price for *now* and a valuation
    date is what that column means on every path.
    """
    inst_made = 0
    inv_txn_made = 0
    for spec in INSTRUMENTS:
        symbol = str(spec["symbol"])
        existing = session.scalar(
            select(Instrument).where(
                Instrument.user_id == user_id,
                Instrument.symbol == symbol,
                Instrument.currency == "INR",
                Instrument.archived_at.is_(None),
            )
        )
        if existing is not None:
            continue
        instrument = Instrument(
            user_id=user_id,
            symbol=symbol,
            name=str(spec["name"]),
            asset_class=str(spec["asset_class"]),
            exchange=str(spec["exchange"]),
            currency="INR",
            # Optional per spec — only the MF carries one, and without it AMFI can never
            # match the row (see the ``isin`` note in app.core.demo_data).
            isin=str(spec["isin"]) if spec.get("isin") else None,
            current_nav=Decimal(str(spec["current_nav"])),
            # Seeded NAVs are as-of the seed date. Left NULL, the showcase rendered a
            # blank valuation age on every /holdings row and no catalogue staleness at
            # all — the honesty signals invisible in the one dataset the public demo shows.
            nav_updated_at=as_valuation_stamp(clock.today()),
        )
        session.add(instrument)
        session.flush()  # assign id for the txn FK
        inst_made += 1
        for t in spec["txns"]:
            session.add(
                InvestmentTransaction(
                    user_id=user_id,
                    instrument_id=instrument.id,
                    date=date_t.fromisoformat(str(t["date"])),
                    transaction_type=str(t["type"]),
                    units=Decimal(str(t["units"])) if "units" in t else Decimal("0"),
                    price_per_unit_native=Decimal(str(t["price"])) if "price" in t else None,
                    amount_native_paise=int(t.get("amount", 0)),
                    fees_native_paise=int(t.get("fee", 0)),
                    fx_rate_to_inr=Decimal("1"),
                    note=str(t["note"]) if t.get("note") else None,
                    pair_id=None,
                )
            )
            inv_txn_made += 1
    return inst_made, inv_txn_made
