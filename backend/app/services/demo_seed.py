"""Direct-DB demo seeder — keeps the demo account's transactions fresh on every boot.

The app lifespan calls :func:`seed_demo_data` on every boot (see :mod:`app.main`),
not just once on an empty DB — that's what keeps the "Try the demo" account's
dashboards non-stale without a separate reseed step. Restarting `main.py` is
now the only way to refresh the demo data; the HTTP-driving manual script this
module used to share its dataset with (``scripts/seed_dev_data.py``) was
deleted once that made it redundant.

Dataset lives in :mod:`app.core.demo_data`:
:func:`app.core.demo_data.build_demo_dataset` materializes a rolling window of
spend/income rows relative to ``clock.today()``. The load-bearing invariants —
merchant normalization, the PRD §F4 fingerprint, and F3 tag learning — are
reused verbatim from their existing single-source-of-truth functions
(``normalize_merchant``, ``transaction_fingerprint``, ``record_tag``);
re-implementing them here would let the fingerprint inputs drift from the POST
route and silently break dedup.

Idempotency is by wipe-and-regenerate, not by diffing: this account carries only
seed data (SEED_DEMO_ON_STARTUP is a dev/demo-only convenience, off by default
for the self-host stacks that hold real data — see ``app.core.demo``), so each
call hard-deletes the demo accounts' existing transactions and reinserts a fresh
window, rather than reconciling row-by-row. Accounts/instruments stay
find-or-create — instrument purchase history is NOT wiped (XIRR needs it since
inception). Network is deliberately untouched — the benchmark NAV backfill
(mfapi) stays in the HTTP script; blocking boot on an external fetch is not
worth it, and benchmark history isn't needed for the core demo.
"""

from __future__ import annotations

from datetime import date as date_t
from decimal import Decimal
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core import clock
from app.core.demo_data import INSTRUMENTS, build_demo_dataset
from app.core.log_config import get_logger
from app.models import Account, Category, Instrument, InvestmentTransaction, Transaction
from app.services.category_service import FALLBACK_CATEGORY_NAME
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
    fallback = FALLBACK_CATEGORY_NAME.lower()
    if fallback not in spend or fallback not in income:
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

    Resolves labels and learns the merchant → category/label memory (spend-typed rows
    only, via :func:`learn_merchant_memory`) BEFORE adding the row — same slot and
    ordering as ``create_transaction``, so the seeder exercises the same path and
    the demo user's ``merchant_tag_map`` / ``merchant_label_map`` are populated for
    a later import to prefill. Labels are linked after a flush assigns the txn id.
    Caller owns the commit — no 409 handler here, because the caller wipes the
    demo accounts' existing rows before regenerating (see
    ``_reset_demo_transactions``), so there is nothing an insert could collide with.
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


def _reset_demo_transactions(
    session: Session, *, user_id: UUID, account_ids: tuple[int, ...]
) -> None:
    """Hard-delete every existing transaction on the demo accounts before the
    caller regenerates the rolling window.

    Null ``transfer_pair_id`` first: that self-referential FK
    (``fk_transactions_transfer_pair_same_user``) has no ``ondelete`` action, so
    deleting one leg of a pair while its partner still points at it would raise
    ``IntegrityError``. Nothing seeded pairs a transfer today, but this keeps the
    wipe safe if F2/F4a demo data ever does. ``transaction_labels`` needs no such
    step — its FKs are ``ondelete="CASCADE"`` and SQLite has
    ``PRAGMA foreign_keys=ON`` (core/db.py), so the label links go with the row.
    """
    session.execute(
        update(Transaction)
        .where(Transaction.user_id == user_id, Transaction.account_id.in_(account_ids))
        .values(transfer_pair_id=None)
    )
    session.execute(
        delete(Transaction).where(
            Transaction.user_id == user_id, Transaction.account_id.in_(account_ids)
        )
    )


def seed_demo_data(session: Session, *, user_id: UUID) -> DemoSeedCounts:
    """Find-or-create the demo accounts/instruments, then wipe and regenerate
    the demo accounts' transactions for a rolling window ending today, all in
    one commit.

    Accounts/instruments are idempotent (find-or-create) as before. Transactions
    are NOT diffed — see the module docstring for why a full wipe-and-regenerate
    is the right call here — so this is safe to call on every boot, not just an
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

    _reset_demo_transactions(session, user_id=user_id, account_ids=(cc.id, bank.id))
    spends, refunds, income = build_demo_dataset(clock.today())

    for row in spends:
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
    for row in refunds:
        _add_transaction(
            session,
            user_id=user_id,
            account_id=cc.id,
            on=date_t.fromisoformat(row.date),
            # A refund IS a spend row with a positive amount (ADR-0009), filed
            # under the SAME category as the spend it reverses — which is what
            # makes the §F4a signed sum net. The seed demonstrates exactly that.
            amount_paise=row.rupees * 100,
            transaction_type="spend",
            merchant_raw=row.merchant,
            category_id=spend_cats.get(row.category.lower(), other_spend),
            labels=row.labels,
        )
        txn_count += 1
    for inc in income:
        # Almost everything is bank-credited salary; card cashback is credited
        # on the card statement itself (real Axis Flipkart behaviour) — see
        # IncomeRow.account in app.core.demo_data.
        account_id = cc.id if inc.account == "card" else bank.id
        _add_transaction(
            session,
            user_id=user_id,
            account_id=account_id,
            on=date_t.fromisoformat(inc.date),
            amount_paise=inc.rupees * 100,  # income = positive
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
