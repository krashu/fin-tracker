"""Direct-DB demo seeder — keeps the demo account's transactions fresh on every boot.

The app lifespan calls :func:`seed_demo_data` on every boot (see :mod:`app.main`),
not just once on an empty DB — that's what keeps the "Try the demo" account's
dashboards non-stale without a separate reseed step. Restarting `main.py` is
now the only way to refresh the demo data; the HTTP-driving manual script this
module used to share its dataset with (``scripts/seed_dev_data.py``) was
deleted once that made it redundant.

Dataset lives in :mod:`app.core.demo_data`:
:func:`app.core.demo_data.build_demo_dataset` materializes a rolling window of
spend/income rows relative to ``clock.today()``, and its siblings the ATM
transfers and card bills written as linked pairs (ADR-0002). The load-bearing invariants —
merchant normalization and the PRD §F4 fingerprint — are
reused verbatim from their existing single-source-of-truth functions
(``normalize_merchant``, ``transaction_fingerprint``); the F3 rule rows are written
directly (:func:`_write_demo_rule_rows`), never through the teach path (ADR-0004
rule 12). Re-implementing the shared functions here would let the fingerprint inputs
drift from the POST route and silently break dedup.

Idempotency is by wipe-and-regenerate, not by diffing: this account carries only
seed data (SEED_DEMO_ON_STARTUP is a dev/demo-only convenience, off by default
for the self-host stacks that hold real data — see ``app.core.demo``), so each
call hard-deletes the demo accounts' existing transactions and reinserts a fresh
window, rather than reconciling row-by-row. Accounts/instruments stay
find-or-create (each card's paying bank is re-set on every call) — instrument
purchase history is NOT wiped (XIRR needs it since inception). Network is
deliberately untouched — the benchmark NAV backfill (mfapi) stays in the HTTP
script; blocking boot on an external fetch is not worth it, and benchmark history
isn't needed for the core demo.
"""

from __future__ import annotations

from collections import Counter
from datetime import date as date_t
from decimal import Decimal
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import clock
from app.core.db_errors import insert_skip_existing
from app.core.demo_data import (
    ACCOUNTS,
    AccountKey,
    build_demo_card_bills,
    build_demo_dataset,
    build_demo_instruments,
    build_demo_transfers,
)
from app.core.log_config import get_logger
from app.models import (
    Account,
    Category,
    FxRateQuote,
    Instrument,
    InvestmentTransaction,
    Label,
    MerchantLabelMap,
    MerchantTagMap,
    Transaction,
)
from app.services.category_service import FALLBACK_CATEGORY_NAME
from app.services.fingerprint import transaction_fingerprint
from app.services.holdings_service import rebuild_realised_lots
from app.services.merchant import normalize_merchant
from app.services.merchant_alias import AliasResolver, load_alias_resolver
from app.services.nav_snapshot_service import as_valuation_stamp
from app.services.reconciliation_service import pair_existing_rows
from app.services.tag_service import should_learn_tag
from app.services.transaction_delete import delete_owned_transactions
from app.services.transaction_labels import link_labels, resolve_label_names

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
    resolver: AliasResolver,
    tag_counts: Counter[tuple[str, int]],
    label_counts: Counter[tuple[str, int]],
    resolved_labels: dict[tuple[str, ...], list[Label]],
    pending_links: list[tuple[Transaction, list[Label]]],
    labels: tuple[str, ...] = (),
) -> Transaction:
    """Build + add one confirmed manual transaction, mirroring the POST route.

    Resolves labels and tallies the row toward the demo user's merchant → category
    and merchant → label rule rows (spend-typed rows with a non-empty canonical
    only — an eligibility filter, not a teach); :func:`_write_demo_rule_rows`
    persists the tallies once at the end. Labels resolve once per distinct label set
    (``resolved_labels``) and are queued on ``pending_links``; :func:`_link_labels`
    links them all after one flush assigns the ids. A flush per labelled row was most
    of the seed's time. Caller owns the commit — no 409 handler here, because the caller wipes the
    demo accounts' existing rows before regenerating (see
    ``_reset_demo_transactions``), so there is nothing an insert could collide with.
    """
    merchant_normalized = normalize_merchant(merchant_raw) if merchant_raw else ""
    resolved: list[Label] = []
    if labels:
        if labels not in resolved_labels:
            resolved_labels[labels] = resolve_label_names(session, user_id=user_id, names=labels)
        resolved = resolved_labels[labels]
    canonical = resolver.canonical(merchant_normalized)
    if should_learn_tag(transaction_type=transaction_type, merchant_normalized=canonical):
        tag_counts[(canonical, category_id)] += 1
        for label in resolved:
            label_counts[(canonical, label.id)] += 1
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
        confirmed_at=clock.naive_utcnow(),
    )
    session.add(txn)
    if resolved:
        pending_links.append((txn, resolved))
    return txn


def _link_labels(session: Session, pending_links: list[tuple[Transaction, list[Label]]]) -> None:
    """Link the queued labels after one flush assigns every txn id. The txns are new,
    so the insert-only :func:`link_labels` applies (no diff lookup per row)."""
    session.flush()
    for txn, labels in pending_links:
        link_labels(
            session, txn_id=txn.id, user_id=txn.user_id, label_ids=[lab.id for lab in labels]
        )


def _transfer_leg(
    *, user_id: UUID, account: Account, on: date_t, amount_paise: int, merchant: str
) -> Transaction:
    """One confirmed, uncategorised, unpaired ``transfer`` row. Transfers feed no F3
    rule, so no tallies."""
    merchant_normalized = normalize_merchant(merchant)
    return Transaction(
        user_id=user_id,
        account_id=account.id,
        date=on,
        amount_paise=amount_paise,
        transaction_type="transfer",
        merchant_raw=merchant,
        merchant_normalized=merchant_normalized,
        category_id=None,
        fingerprint=transaction_fingerprint(
            txn_date=on,
            amount_paise=amount_paise,
            normalized_merchant=merchant_normalized,
            account_id=account.id,
        ),
        source="manual",
        import_batch_id=None,
        confirmed_at=clock.naive_utcnow(),
    )


def _add_transfer_pair(
    session: Session, *, user_id: UUID, source: Account, dest: Account, on: date_t, rupees: int
) -> None:
    """Two linked ``transfer`` rows for one movement, shaped as the F2 manual transfer
    route writes them (``api/v1/transactions.py`` ``create_transfer``): server-signed
    legs, no category, "Transfer to/from {name}" merchants, linked both ways after a
    flush assigns the ids (ADR-0002)."""
    out = _transfer_leg(
        user_id=user_id,
        account=source,
        on=on,
        amount_paise=-rupees * 100,
        merchant=f"Transfer to {dest.name}",
    )
    in_ = _transfer_leg(
        user_id=user_id,
        account=dest,
        on=on,
        amount_paise=rupees * 100,
        merchant=f"Transfer from {source.name}",
    )
    session.add_all((out, in_))
    session.flush()
    out.transfer_pair_id, in_.transfer_pair_id = in_.id, out.id


# Both legs' merchants match the F4a card-bill vocabulary: the bank leg
# ``is_card_bill_debit``, the card leg ``is_cc_payment`` (reconciliation_service).
_CARD_PAYMENT_MERCHANT = "Payment Received - Thank You"


def _add_card_bills(
    session: Session, *, user_id: UUID, accounts: dict[AccountKey, Account], today: date_t
) -> int:
    """Each card bill as its two statement rows, in the state a statement import commits
    them: uncategorised, unpaired ``transfer`` legs (ADR-0002 rules 7b and 7c), the bank's
    "Credit Card Payment" debit and the card's "Payment Received" credit. Then every bill
    but the one marked unlinked is paired by :func:`pair_existing_rows`, as F4a's
    auto-link pairs them, so an unlink in the demo restores exactly what it would on real
    data. Returns the rows written."""
    pairs: list[tuple[Transaction, Transaction, bool]] = []
    for bill in build_demo_card_bills(today):
        card = accounts[bill.card]
        on = date_t.fromisoformat(bill.date)
        debit = _transfer_leg(
            user_id=user_id,
            account=accounts[bill.bank],
            on=on,
            amount_paise=-bill.rupees * 100,
            merchant=f"Credit Card Payment - {card.name}",
        )
        credit = _transfer_leg(
            user_id=user_id,
            account=card,
            on=on,
            amount_paise=bill.rupees * 100,
            merchant=_CARD_PAYMENT_MERCHANT,
        )
        session.add_all((debit, credit))
        pairs.append((debit, credit, bill.linked))
    session.flush()
    for debit, credit, linked in pairs:
        if linked:
            pair_existing_rows(session, debit, credit)
    return 2 * len(pairs)


def _write_demo_rule_rows(
    session: Session,
    *,
    user_id: UUID,
    tag_counts: Counter[tuple[str, int]],
    label_counts: Counter[tuple[str, int]],
) -> None:
    """Write the demo user's rule rows straight into the maps (ADR-0004 rule 12).

    ``hit_count`` is the number of dataset rows behind each ``(canonical, category)``
    / ``(canonical, label)`` pair. A missing row is inserted at that count; an
    existing one is only ever *raised* to it (a seed-dictionary row at 0 becomes
    "learned", a user-bumped row keeps its higher count), never lowered, and
    ``pinned`` is never touched. Re-running on the same dataset therefore changes
    nothing — unlike teaching on every boot, which added the dataset's counts again.
    """
    existing_tags = {
        (r.merchant_normalized, r.category_id): r
        for r in session.scalars(select(MerchantTagMap).where(MerchantTagMap.user_id == user_id))
    }
    for (canonical, category_id), count in tag_counts.items():
        tag_row = existing_tags.get((canonical, category_id))
        if tag_row is None:
            session.add(
                MerchantTagMap(
                    user_id=user_id,
                    merchant_normalized=canonical,
                    category_id=category_id,
                    hit_count=count,
                )
            )
        elif tag_row.hit_count < count:
            tag_row.hit_count = count

    existing_labels = {
        (r.merchant_normalized, r.label_id): r
        for r in session.scalars(
            select(MerchantLabelMap).where(MerchantLabelMap.user_id == user_id)
        )
    }
    for (canonical, label_id), count in label_counts.items():
        label_row = existing_labels.get((canonical, label_id))
        if label_row is None:
            session.add(
                MerchantLabelMap(
                    user_id=user_id,
                    merchant_normalized=canonical,
                    label_id=label_id,
                    hit_count=count,
                )
            )
        elif label_row.hit_count < count:
            label_row.hit_count = count


def _reset_demo_transactions(
    session: Session, *, user_id: UUID, account_ids: tuple[int, ...]
) -> None:
    """Hard-delete every transaction on the demo accounts through the one shared delete
    (:func:`app.services.transaction_delete.delete_owned_transactions`), which releases any kept
    partner (ADR-0002 rules 5 and 9) and nulls kept refunds' links into the wiped rows
    (ADR-0009 rule 13). Labels cascade. The caller regenerates the rolling window afterwards.
    """
    wiped = session.scalars(
        select(Transaction.id).where(
            Transaction.user_id == user_id, Transaction.account_id.in_(account_ids)
        )
    ).all()
    delete_owned_transactions(session, user_id=user_id, ids=wiped)


def seed_demo_data(session: Session, *, user_id: UUID) -> DemoSeedCounts:
    """Find-or-create the demo accounts/instruments, then wipe and regenerate
    the demo accounts' transactions for a rolling window ending today, all in
    one commit.

    Accounts/instruments are idempotent (find-or-create); each card's paying bank
    (``parent_account_id``) is re-set every call. Transactions
    are NOT diffed — see the module docstring for why a full wipe-and-regenerate
    is the right call here — so this is safe to call on every boot, not just an
    empty DB. Returns the inserted counts for logging.
    """
    accounts: dict[AccountKey, Account] = {
        spec.key: _get_or_create_account(
            session,
            user_id=user_id,
            name=spec.name,
            type=spec.type,
            issuer=spec.issuer,
            last4=spec.last4,
            currency="INR",
            opening_balance_paise=0,
        )
        for spec in ACCOUNTS
    }
    for spec in ACCOUNTS:
        # Set on an existing account too, so a DB seeded before cards had a paying
        # bank picks the link up.
        if spec.parent is not None:
            accounts[spec.key].parent_account_id = accounts[spec.parent].id

    spend_cats, income_cats = _categories_by_kind(session, user_id=user_id)
    other_spend, other_income = spend_cats["other"], income_cats["other"]
    txn_count = 0
    resolver = load_alias_resolver(session, user_id=user_id)
    tag_counts: Counter[tuple[str, int]] = Counter()
    label_counts: Counter[tuple[str, int]] = Counter()
    resolved_labels: dict[tuple[str, ...], list[Label]] = {}
    pending_links: list[tuple[Transaction, list[Label]]] = []

    _reset_demo_transactions(
        session, user_id=user_id, account_ids=tuple(a.id for a in accounts.values())
    )
    # Read once: a seed spanning midnight at a month's end must not build the spends for
    # one window and the transfers and bills for the next.
    today = clock.today()
    spends, refunds, income = build_demo_dataset(today)

    by_row: dict[tuple[AccountKey, str, str], Transaction] = {}
    refund_links: list[tuple[Transaction, Transaction]] = []
    for row in spends:
        by_row[(row.account, row.date, row.merchant)] = _add_transaction(
            session,
            user_id=user_id,
            account_id=accounts[row.account].id,
            on=date_t.fromisoformat(row.date),
            amount_paise=-row.rupees * 100,  # spend = negative
            transaction_type="spend",
            merchant_raw=row.merchant,
            category_id=spend_cats.get(row.category.lower(), other_spend),
            resolver=resolver,
            tag_counts=tag_counts,
            label_counts=label_counts,
            resolved_labels=resolved_labels,
            pending_links=pending_links,
            labels=row.labels,
        )
        txn_count += 1
    for row in refunds:
        refund = _add_transaction(
            session,
            user_id=user_id,
            account_id=accounts[row.account].id,
            on=date_t.fromisoformat(row.date),
            # A refund IS a spend row with a positive amount (ADR-0009), filed
            # under the SAME category as the spend it reverses — which is what
            # makes the §F4a signed sum net. The seed demonstrates exactly that.
            amount_paise=row.rupees * 100,
            transaction_type="spend",
            merchant_raw=row.merchant,
            category_id=spend_cats.get(row.category.lower(), other_spend),
            resolver=resolver,
            tag_counts=tag_counts,
            label_counts=label_counts,
            resolved_labels=resolved_labels,
            pending_links=pending_links,
            labels=row.labels,
        )
        if row.refund_of is not None:
            refund_links.append((refund, by_row[(row.account, *row.refund_of)]))
        txn_count += 1
    for inc in income:
        # Income posts to the account the row names (IncomeRow.account in
        # app.core.demo_data, the bank by default). Card cashback is not here: it is a
        # positive spend among the refunds above (ADR-0013).
        _add_transaction(
            session,
            user_id=user_id,
            account_id=accounts[inc.account].id,
            on=date_t.fromisoformat(inc.date),
            amount_paise=inc.rupees * 100,  # income = positive
            transaction_type="income",
            merchant_raw=inc.source,
            category_id=income_cats.get(inc.category.lower(), other_income),
            resolver=resolver,
            tag_counts=tag_counts,
            label_counts=label_counts,
            resolved_labels=resolved_labels,
            pending_links=pending_links,
            labels=inc.labels,
        )
        txn_count += 1

    for move in build_demo_transfers(today):
        _add_transfer_pair(
            session,
            user_id=user_id,
            source=accounts[move.source],
            dest=accounts[move.dest],
            on=date_t.fromisoformat(move.date),
            rupees=move.rupees,
        )
        txn_count += 2
    txn_count += _add_card_bills(session, user_id=user_id, accounts=accounts, today=today)

    _link_labels(session, pending_links)  # flushes, so every refund's original has an id
    for refund, original in refund_links:
        refund.refund_of_id = original.id
    _write_demo_rule_rows(
        session, user_id=user_id, tag_counts=tag_counts, label_counts=label_counts
    )

    _seed_fx_rates(session, today=today)
    inst_count, inv_txn_count = _seed_investments(session, user_id=user_id, today=today)

    session.flush()
    return DemoSeedCounts(
        accounts=len(accounts),
        transactions=txn_count,
        instruments=inst_count,
        investment_transactions=inv_txn_count,
    )


def _seed_fx_rates(session: Session, *, today: date_t) -> None:
    """Seed USD->INR rates for the demo dataset (today's rate + demo txn dates).

    Ensures demo USD holdings (AAPL, VOO) have valid INR conversions for %Alloc and XIRR
    without requiring an initial external Frankfurter fetch.
    """
    rates_by_date: dict[date_t, Decimal] = {
        today: Decimal("89.50"),
    }
    for spec in build_demo_instruments(today):
        if spec.get("currency") == "USD":
            for t in spec.get("txns", []):
                if "date" in t and "fx" in t:
                    rates_by_date[date_t.fromisoformat(str(t["date"]))] = Decimal(str(t["fx"]))

    rows: list[dict[str, object]] = [
        {
            "date": d,
            "from_currency": "USD",
            "to_currency": "INR",
            "rate": rate,
            "source": "demo_seed",
        }
        for d, rate in rates_by_date.items()
    ]
    insert_skip_existing(
        session,
        FxRateQuote,
        rows,
        conflict_cols=["from_currency", "to_currency", "date"],
        label="fx_rates",
    )


def _seed_investments(session: Session, *, user_id: UUID, today: date_t) -> tuple[int, int]:
    """Find-or-create each instrument + its txns. Returns (instruments, txns) made.

    Only seeds an instrument's txns when the instrument is newly created (manual
    investment txns have no dedup key), mirroring the HTTP script's gate. INR
    instruments stamp ``fx_rate_to_inr=1`` — the identity conversion (PRD §F7); a USD
    instrument's txns stamp the dataset's literal rate for each date.
    ``fingerprint`` stays NULL (manual rows aren't CAS-deduped). ``nav_updated_at`` is
    the seed date, because the seeded NAV is a made-up price for *now* and a valuation
    date is what that column means on every path.
    """
    inst_made = 0
    inv_txn_made = 0
    for spec in build_demo_instruments(today):
        symbol = str(spec["symbol"])
        currency = str(spec.get("currency", "INR"))
        existing = session.scalar(
            select(Instrument).where(
                Instrument.user_id == user_id,
                Instrument.symbol == symbol,
                Instrument.currency == currency,
                Instrument.archived_at.is_(None),
            )
        )
        if existing is not None:
            if existing.nav_updated_at is not None and existing.nav_updated_at < as_valuation_stamp(
                today
            ):
                existing.nav_updated_at = as_valuation_stamp(today)
            continue
        instrument = Instrument(
            user_id=user_id,
            symbol=symbol,
            name=str(spec["name"]),
            asset_class=str(spec["asset_class"]),
            exchange=str(spec["exchange"]),
            currency=currency,
            # Optional per spec — only the MF carries one, and without it AMFI can never
            # match the row (see the ``isin`` note in app.core.demo_data).
            isin=str(spec["isin"]) if spec.get("isin") else None,
            current_nav=Decimal(str(spec["current_nav"])),
            # Seeded NAVs are as-of the seed date. Left NULL, the showcase rendered a
            # blank valuation age on every /holdings row and no catalogue staleness at
            # all — the honesty signals invisible in the one dataset the public demo shows.
            nav_updated_at=as_valuation_stamp(today),
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
                    fx_rate_to_inr=Decimal(str(t.get("fx", "1"))),
                    note=str(t["note"]) if t.get("note") else None,
                    pair_id=None,
                )
            )
            inv_txn_made += 1
        # ADR-0015 rule 5: the demo's sells (e.g. INFY) own realised lots like any other.
        rebuild_realised_lots(session, user_id=user_id, instrument_id=instrument.id)
    return inst_made, inv_txn_made
