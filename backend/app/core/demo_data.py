"""Demo dataset — single source of truth for the demo/sample data.

Pure data, no I/O. The sole consumer is :mod:`app.services.demo_seed`, which
writes it straight to the ORM in the app lifespan, on EVERY boot (PRD §Users &
access v2 demo account) — not just an empty DB. A restart is what keeps the
"Try the demo" account looking current. (A second, HTTP-driving consumer —
``scripts/seed_dev_data.py`` — existed before boot-time seeding refreshed
itself on every restart; it was deleted once that made it redundant.)

Rupee magnitudes here are whole ₹; each writer converts to paise (×100).
Investment ``amount``/``fee`` are already native paise; ``units``/``price`` are
exact-decimal strings.

``labels`` (F3a user tags) are populated on a representative subset of spends so
the tag feature — chips, autocomplete, and the board tag filter — is visibly
demonstrated without every row looking annotated. Empty tuple = no labels.

## Rolling window, not fixed dates

The spend/income dataset used to be a flat list of absolute ISO dates, which
goes stale the moment "today" walks past the last seeded month. It is now a
12-slot cyclic monthly TEMPLATE (day-of-month + merchant/category/rupees/labels)
materialized by :func:`build_demo_dataset` relative to an ``anchor`` date —
normally ``clock.today()``, supplied by the caller so this module stays pure
and testable. Slot 0 is the anchor's own (current, partial) month; slot 11 is
the oldest of the 12-month narrative cycle. A window wider than 12 months (the
default is :data:`DEMO_WINDOW_MONTHS`) just repeats the earliest slots — fine
for a synthetic demo, nobody is comparing year-over-year.

On top of the slots, :data:`_RECURRING` lays down the steady monthly life of one
salaried household across :data:`ACCOUNTS` (two cards, a bank account and a cash
wallet): rent, an EMI and bills off the bank, everyday spends on the cards, small
cash spends. Those repeat every month (or in named calendar months, like an annual
premium or Diwali), with amounts jittered per calendar month. The bank pays each
card's previous month in full (:func:`build_demo_card_bills`) and funds the cash
wallet (:func:`build_demo_transfers`); refunds name the spend they reverse.

``tests/test_migration_parity.py`` hardcodes 12 merchants this dataset teaches (F3
auto-tag learning) as colliding with the ADR-0011 seed dictionary; renaming or
dropping one of those 12 breaks that test. Add new merchants freely; don't rename
the existing ones.

The current (slot 0) month is truncated to ``day <= anchor.day`` so a demo
session never shows a future-dated transaction.
"""

from __future__ import annotations

import calendar
import random
from datetime import date as date_t
from decimal import Decimal
from typing import Any, Literal, NamedTuple

# Trailing window the boot-time seeder keeps in place, in months (inclusive of
# the current partial month). Picked as a bit more than a year so the rolling
# window always has full-year context, not just a bare 12.
DEMO_WINDOW_MONTHS = 14

# Which demo account a row posts to. "card" is the original Axis card and stays the
# default so the one-off templates below read as before.
AccountKey = Literal["card", "bank", "card2", "cash"]


class AccountSpec(NamedTuple):
    """One demo account. ``parent`` names the bank account that pays a card's bill
    (``Account.parent_account_id``), on ``bill_day`` each month. A card's ``issuer``
    must have a statement parser, or the accounts API refuses every edit of it."""

    key: AccountKey
    name: str
    type: Literal["credit_card", "bank", "cash"]
    issuer: str | None
    # Placeholder last-4s: the demo account is a public "Try the demo" login.
    # NEVER use a real card's last-4; the demo DB carries only synthetic ids.
    last4: str | None
    parent: AccountKey | None = None
    bill_day: int | None = None


class AllocationTargetSpec(NamedTuple):
    """Target portfolio allocation weight for one asset class or instrument."""

    target_type: Literal["asset_class", "instrument", "tag"]
    target_key: str
    target_bps: int
    rebalance_threshold_bps: int = 500


DEMO_ALLOCATION_TARGETS: list[AllocationTargetSpec] = [
    AllocationTargetSpec("asset_class", "indian_equity", 2500, 500),
    AllocationTargetSpec("asset_class", "indian_mf", 2500, 500),
    AllocationTargetSpec("asset_class", "us_equity", 2500, 500),
    AllocationTargetSpec("asset_class", "debt", 1500, 500),
    AllocationTargetSpec("asset_class", "gold", 1000, 500),
]


class InvestmentAccountSpec(NamedTuple):
    """Demo investment account partition (ADR-0019)."""

    key: str
    name: str
    account_type: Literal["demat", "mf_folio", "us_broker", "bank_fd", "nps", "manual"]
    provider: str
    external_account_ref: str
    currency: Literal["INR", "USD"]


INVESTMENT_ACCOUNTS: list[InvestmentAccountSpec] = [
    InvestmentAccountSpec("zerodha", "Zerodha Demat", "demat", "zerodha", "zerodha_demo", "INR"),
    InvestmentAccountSpec("groww", "Groww Mutual Funds", "mf_folio", "groww", "groww_demo", "INR"),
    InvestmentAccountSpec(
        "indmoney", "INDmoney US Brokerage", "us_broker", "indmoney", "indmoney_demo", "USD"
    ),
    InvestmentAccountSpec(
        "hdfc_fd", "HDFC Fixed Deposits", "bank_fd", "hdfc", "hdfc_fd_demo", "INR"
    ),
]


ACCOUNTS: list[AccountSpec] = [
    # Bills after the 1st's salary, apart so the two read as two payments.
    AccountSpec("card", "Axis Flipkart", "credit_card", "axis", "4321", "bank", 18),
    AccountSpec("bank", "HDFC Savings", "bank", "hdfc", None),
    AccountSpec("card2", "ICICI Amazon Pay", "credit_card", "icici", "8765", "bank", 22),
    AccountSpec("cash", "Cash wallet", "cash", None, None),
]


class SpendRow(NamedTuple):
    """A concrete, dated card spend / refund row. ``rupees`` is a positive
    magnitude; the writer signs it (spend negative, refund positive). ``labels``
    are F3a user tags (get-or-created on seed). ``refund_of`` is the ``(date,
    merchant)`` of the spend a refund reverses, on the same account (ADR-0009
    rule 13); ``None`` on a spend and on cashback, which reverses nothing."""

    date: str
    merchant: str
    category: str
    rupees: int
    labels: tuple[str, ...] = ()
    account: AccountKey = "card"
    refund_of: tuple[str, str] | None = None


class IncomeRow(NamedTuple):
    """A concrete, dated income row. ``rupees`` is positive. ``account`` picks
    which demo account it posts to — the bank, unless a row says otherwise.
    (Card cashback is not income: it is a positive spend on the card, ADR-0013.)"""

    date: str
    source: str
    category: str
    rupees: int
    labels: tuple[str, ...] = ()
    account: AccountKey = "bank"


class TransferRow(NamedTuple):
    """A concrete, dated money movement between two demo accounts, written as a
    linked pair of ``transfer`` rows (ADR-0002). ``rupees`` is positive."""

    date: str
    source: AccountKey
    dest: AccountKey
    rupees: int


class CardBillRow(NamedTuple):
    """One month's card bill, paid in full from the card's paying ``bank``: the bank's
    "Credit Card Payment" debit and the card's "Payment Received" credit, both on
    ``date``. ``linked`` is False for the one bill left for the user to link by hand."""

    date: str
    card: AccountKey
    bank: AccountKey
    rupees: int
    linked: bool = True


class _SpendTemplate(NamedTuple):
    """One recurring spend/refund slot. ``slot`` counts months back from the
    anchor's month (0 = current month, 11 = oldest of the core cycle). A refund's
    ``refund_of`` is the ``(slot, day, merchant)`` of the spend template it reverses."""

    slot: int
    day: int
    merchant: str
    category: str
    rupees: int
    labels: tuple[str, ...] = ()
    account: AccountKey = "card"
    refund_of: tuple[int, int, str] | None = None


class _IncomeTemplate(NamedTuple):
    slot: int
    day: int
    source: str
    category: str
    rupees: int
    labels: tuple[str, ...] = ()
    account: AccountKey = "bank"


class _Recurring(NamedTuple):
    """A spend that repeats on the same day of every month (or only in the calendar
    ``months`` listed, 1-12). ``jitter_pct`` varies the amount by up to that percentage,
    seeded from the calendar month, so a given month's amount never changes as the
    window rolls forward."""

    merchant: str
    category: str
    account: AccountKey
    day: int
    rupees: int
    jitter_pct: int = 0
    labels: tuple[str, ...] = ()
    months: tuple[int, ...] = ()


# The steady monthly life: bills and rent off the bank, day-to-day spends on the two
# cards, small cash spends. Sized with the salary below so every month nets positive
# except the deficit slot (see _SPEND_TEMPLATE), and neither the bank nor the cash
# wallet goes below zero, card bills included; tests/test_demo_data.py locks both.
_RECURRING: list[_Recurring] = [
    # Bank: rent, loan, bills.
    _Recurring("Rent - NoBroker", "Rent & Maintenance", "bank", 3, 28000),
    _Recurring("Society Maintenance", "Rent & Maintenance", "bank", 5, 3500),
    _Recurring("HDFC Car Loan EMI", "Loan EMIs", "bank", 5, 14500),
    _Recurring("ACT Fibernet", "Bills & Utilities", "bank", 8, 1179),
    _Recurring("BESCOM Electricity", "Bills & Utilities", "bank", 12, 1800, 25),
    _Recurring("Indane Gas", "Bills & Utilities", "bank", 16, 1100, 5, months=(1, 3, 5, 7, 9, 11)),
    _Recurring("LIC Premium", "Doctor & Diagnostics", "bank", 10, 24000, months=(3,)),
    # Cash wallet.
    _Recurring("Maid salary", "Rent & Maintenance", "cash", 1, 4000),
    _Recurring("Chai stall", "Dining Out & Delivery", "cash", 6, 60, 30),
    _Recurring("Sabzi vendor", "Groceries", "cash", 9, 450, 30),
    _Recurring("Auto rickshaw", "Daily Commute", "cash", 14, 120, 30),
    # Axis card: groceries, food, rides, subscriptions. (Coffee and extra orders come
    # from _SPEND_TEMPLATE.)
    _Recurring("Big Basket", "Groceries", "card", 7, 1800, 20),
    _Recurring("Swiggy", "Dining Out & Delivery", "card", 6, 600, 35, ("online",)),
    _Recurring("Zomato", "Dining Out & Delivery", "card", 13, 750, 30, ("online",)),
    _Recurring("Swiggy Instamart", "Groceries", "card", 27, 900, 30, ("online",)),
    _Recurring("Uber", "Daily Commute", "card", 11, 380, 30),
    _Recurring("Netflix", "Streaming & Subscriptions", "card", 20, 649),
    _Recurring("Spotify", "Streaming & Subscriptions", "card", 21, 149),
    # ICICI Amazon Pay: fuel, phone, transit, pharmacy, outings, Diwali.
    _Recurring("Indian Oil", "Vehicle Service & Fuel", "card2", 9, 2500, 15),
    _Recurring("Airtel", "Bills & Utilities", "card2", 27, 1199),
    _Recurring("Metro", "Daily Commute", "card2", 15, 500),
    _Recurring(
        "Apollo Pharmacy",
        "Pharmacy & Medicines",
        "card2",
        26,
        600,
        30,
        months=(1, 3, 5, 7, 9, 11),
    ),
    _Recurring(
        "BookMyShow", "Events & Outings", "card2", 23, 900, 25, ("weekend",), (2, 4, 6, 8, 10, 12)
    ),
    _Recurring("Tanishq", "Clothing & Personal", "card2", 20, 18000, 0, ("festive",), (10,)),
]

# One-off and extra card spends across the 12-slot cycle, on top of _RECURRING.
# Merchants recur (Big Basket, Swiggy, Uber, …) so the top-merchants aggregate ranks
# meaningfully; slot 9 is a deliberate deficit slot (a Travel + Shopping spike pushes
# spend above the salary) so the cashflow net line dips below 0 for that month.
_SPEND_TEMPLATE: list[_SpendTemplate] = [
    # slot 11 (oldest)
    _SpendTemplate(11, 5, "Big Basket", "Groceries", 2800),
    _SpendTemplate(11, 10, "Swiggy", "Dining Out & Delivery", 650, ("online",)),
    _SpendTemplate(11, 14, "Uber", "Daily Commute", 380),
    _SpendTemplate(11, 24, "Amazon", "Shopping", 1500, ("online",)),
    # slot 10
    _SpendTemplate(10, 6, "Big Basket", "Groceries", 3100),
    _SpendTemplate(10, 11, "Zomato", "Dining Out & Delivery", 720, ("online",)),
    _SpendTemplate(10, 15, "Metro", "Daily Commute", 250),
    _SpendTemplate(10, 26, "Apollo Pharmacy", "Pharmacy & Medicines", 540),
    # slot 9 — deficit slot (spend > salary → net < 0)
    _SpendTemplate(9, 4, "Big Basket", "Groceries", 3600),
    _SpendTemplate(9, 9, "Swiggy", "Dining Out & Delivery", 980, ("online",)),
    _SpendTemplate(9, 12, "MakeMyTrip", "Flights & Trains", 32000, ("travel", "goa")),
    _SpendTemplate(9, 18, "Croma", "Electronics & Home", 18500, ("festive",)),
    _SpendTemplate(9, 22, "Myntra", "Clothing & Personal", 6200, ("festive",)),
    _SpendTemplate(9, 25, "Uber", "Daily Commute", 720, ("travel",)),
    # slot 8
    _SpendTemplate(8, 5, "Big Basket", "Groceries", 2900),
    _SpendTemplate(8, 9, "Blue Tokai", "Dining Out & Delivery", 480, ("restaurant",)),
    _SpendTemplate(8, 14, "Swiggy", "Dining Out & Delivery", 610, ("online",)),
    _SpendTemplate(8, 16, "Metro", "Daily Commute", 300),
    _SpendTemplate(8, 23, "BookMyShow", "Events & Outings", 900, ("weekend",)),
    # slot 7
    _SpendTemplate(7, 6, "Big Basket", "Groceries", 3400),
    _SpendTemplate(7, 11, "Zomato", "Dining Out & Delivery", 850, ("online",)),
    _SpendTemplate(7, 15, "Uber", "Daily Commute", 560),
    _SpendTemplate(7, 19, "Amazon", "Shopping", 3200, ("online", "gifts")),
    # slot 6
    _SpendTemplate(6, 5, "Big Basket", "Groceries", 3000),
    _SpendTemplate(6, 10, "Swiggy", "Dining Out & Delivery", 700, ("online",)),
    _SpendTemplate(6, 14, "Metro", "Daily Commute", 280),
    _SpendTemplate(6, 25, "Apollo Pharmacy", "Pharmacy & Medicines", 620),
    # slot 5
    _SpendTemplate(5, 6, "Big Basket", "Groceries", 3300),
    _SpendTemplate(5, 11, "Blue Tokai", "Dining Out & Delivery", 520, ("restaurant",)),
    _SpendTemplate(5, 15, "Uber", "Daily Commute", 410),
    _SpendTemplate(5, 19, "Amazon", "Shopping", 2100, ("online",)),
    _SpendTemplate(5, 8, "Manipal Hospitals", "Doctor & Diagnostics", 9500, (), "card2"),
    # slot 4
    _SpendTemplate(4, 5, "Big Basket", "Groceries", 3500),
    _SpendTemplate(4, 10, "Zomato", "Dining Out & Delivery", 890, ("online",)),
    _SpendTemplate(4, 14, "Metro", "Daily Commute", 320),
    _SpendTemplate(4, 18, "Myntra", "Clothing & Personal", 4200),
    # slot 3
    _SpendTemplate(3, 4, "Big Basket", "Groceries", 3200),
    _SpendTemplate(3, 9, "Swiggy", "Dining Out & Delivery", 890, ("online",)),
    _SpendTemplate(3, 15, "Uber", "Daily Commute", 450),
    _SpendTemplate(3, 22, "Amazon", "Shopping", 2400, ("online",)),
    _SpendTemplate(3, 27, "Apollo Pharmacy", "Pharmacy & Medicines", 780),
    # slot 2
    _SpendTemplate(2, 3, "Blue Tokai", "Dining Out & Delivery", 520, ("restaurant",)),
    _SpendTemplate(2, 8, "Metro", "Daily Commute", 300),
    _SpendTemplate(2, 14, "Big Basket", "Groceries", 4100),
    _SpendTemplate(2, 25, "Croma", "Electronics & Home", 1200),
    # slot 1
    _SpendTemplate(1, 3, "Blue Tokai", "Dining Out & Delivery", 450, ("restaurant",)),
    _SpendTemplate(1, 7, "Metro card", "Daily Commute", 180),
    _SpendTemplate(1, 9, "Big Basket", "Groceries", 1200),
    _SpendTemplate(1, 12, "Swiggy", "Dining Out & Delivery", 895, ("online",)),
    # slot 0 (current month) — truncated to day <= anchor.day at generation time
    _SpendTemplate(0, 4, "Big Basket", "Groceries", 2600),
    _SpendTemplate(0, 9, "Swiggy", "Dining Out & Delivery", 740, ("online",)),
    _SpendTemplate(0, 14, "Uber", "Daily Commute", 390),
]

# Card credits: `spend`-typed with a POSITIVE amount. A refund is not its own type
# (ADR-0009) and sits in the same category as the spend it offsets, so the §F4a
# signed sum nets, and links to that spend through ``refund_of``. Slot 9's refund
# partially returns that slot's deficit-driving shopping spike (still leaves slot 9
# in deficit); slot 2's returns the previous month's Amazon order.
# Card cashback is a rebate on spending, a positive spend in "Cashback" (ADR-0013),
# credited on the card statement itself (real Axis Flipkart behaviour).
_REFUND_TEMPLATE: list[_SpendTemplate] = [
    _SpendTemplate(
        9,
        28,
        "Myntra refund",
        "Clothing & Personal",
        2000,
        ("festive",),
        refund_of=(9, 22, "Myntra"),
    ),
    _SpendTemplate(8, 28, "Card Cashback", "Cashback", 350),
    _SpendTemplate(7, 17, "Uber refund", "Daily Commute", 180, refund_of=(7, 15, "Uber")),
    _SpendTemplate(6, 30, "Card Cashback", "Cashback", 420),
    _SpendTemplate(
        2, 18, "Amazon refund", "Shopping", 600, ("online",), refund_of=(3, 22, "Amazon")
    ),
    _SpendTemplate(2, 29, "Croma refund", "Electronics & Home", 1200, refund_of=(2, 25, "Croma")),
]

# Bank income: salary every slot + a year-end and an appraisal bonus (Salary) and
# two freelancing payouts, so income-vs-spend isn't a flat band.
_INCOME_TEMPLATE: list[_IncomeTemplate] = [
    _IncomeTemplate(11, 1, "Acme Payroll", "Salary", 110000),
    _IncomeTemplate(10, 1, "Acme Payroll", "Salary", 110000),
    _IncomeTemplate(10, 18, "Upwork", "Freelancing", 12000),
    _IncomeTemplate(9, 1, "Acme Payroll", "Salary", 110000),
    _IncomeTemplate(8, 1, "Acme Payroll", "Salary", 110000),
    _IncomeTemplate(7, 1, "Acme Payroll", "Salary", 110000),
    _IncomeTemplate(7, 15, "Acme Year-End Bonus", "Salary", 90000),
    _IncomeTemplate(6, 1, "Acme Payroll", "Salary", 110000),
    _IncomeTemplate(5, 1, "Acme Payroll", "Salary", 110000),
    _IncomeTemplate(5, 22, "Consulting", "Freelancing", 15000),
    _IncomeTemplate(4, 1, "Acme Payroll", "Salary", 110000),
    _IncomeTemplate(4, 20, "Acme Appraisal Bonus", "Salary", 60000),
    _IncomeTemplate(3, 1, "Acme Payroll", "Salary", 110000),
    _IncomeTemplate(2, 1, "Acme Payroll", "Salary", 110000),
    _IncomeTemplate(1, 1, "Acme Payroll", "Salary", 110000),
    _IncomeTemplate(0, 1, "Acme Payroll", "Salary", 110000),
]


# The cash wallet's only inflow: an ATM withdrawal on the 1st, the same day as the
# maid's salary it mostly pays for.
_ATM_DAY, _ATM_RUPEES = 1, 6000


def _add_months(anchor: date_t, months_back: int) -> date_t:
    """The first of the month that is ``months_back`` months before ``anchor``'s
    own month (0 = anchor's own month)."""
    total = anchor.year * 12 + (anchor.month - 1) - months_back
    year, month0 = divmod(total, 12)
    return date_t(year, month0 + 1, 1)


def _day_in_month(year: int, month: int, day: int) -> date_t:
    """``day`` clamped to that month's actual length (e.g. a day-30 template
    row lands on Feb 28/29, not a ``ValueError``)."""
    last_day = calendar.monthrange(year, month)[1]
    return date_t(year, month, min(day, last_day))


def _jittered(rec: _Recurring, year: int, month: int) -> int:
    """``rec.rupees`` varied by up to ``rec.jitter_pct``, the same for a given calendar
    month on every run (``random.Random`` seeded from a string is deterministic)."""
    if not rec.jitter_pct:
        return rec.rupees
    rng = random.Random(f"{year}-{month}-{rec.merchant}-{rec.day}")
    return round(rec.rupees * (1 + rng.uniform(-rec.jitter_pct, rec.jitter_pct) / 100))


def _original(
    tmpl: _SpendTemplate, anchor: date_t, months_back: int, window_months: int
) -> tuple[str, str] | None:
    """The ``(date, merchant)`` of the spend a refund template reverses, or ``None``
    when it reverses nothing or that spend's month is outside the window. The
    original is always earlier, so a refund on or before the anchor has its original."""
    if tmpl.refund_of is None:
        return None
    slot, day, merchant = tmpl.refund_of
    original_back = months_back + slot - tmpl.slot
    if original_back >= window_months:
        return None
    month_start = _add_months(anchor, original_back)
    return _day_in_month(month_start.year, month_start.month, day).isoformat(), merchant


def build_demo_dataset(
    anchor: date_t, *, window_months: int = DEMO_WINDOW_MONTHS
) -> tuple[list[SpendRow], list[SpendRow], list[IncomeRow]]:
    """Materialize ``(spends, refunds, income)`` dated rows for the trailing
    ``window_months`` ending at ``anchor``'s month.

    Pure function of ``anchor`` — callers supply ``clock.today()`` (never
    computed in here) so this stays deterministic and testable. The template
    cycles every 12 slots (``months_back % 12``); a wider window just repeats
    the earliest slots again further back. Any row that would land after
    ``anchor`` (only possible in the current, slot-0 month) is dropped so a
    freshly-synced demo account never shows a future-dated transaction.
    """
    spends: list[SpendRow] = []
    refunds: list[SpendRow] = []
    income: list[IncomeRow] = []
    for months_back in range(window_months - 1, -1, -1):
        month_start = _add_months(anchor, months_back)
        slot = months_back % 12
        for rec in _RECURRING:
            if rec.months and month_start.month not in rec.months:
                continue
            on = _day_in_month(month_start.year, month_start.month, rec.day)
            if on > anchor:
                continue
            spends.append(
                SpendRow(
                    on.isoformat(),
                    rec.merchant,
                    rec.category,
                    _jittered(rec, on.year, on.month),
                    rec.labels,
                    rec.account,
                )
            )
        for tmpl in _SPEND_TEMPLATE:
            if tmpl.slot != slot:
                continue
            on = _day_in_month(month_start.year, month_start.month, tmpl.day)
            if on > anchor:
                continue
            spends.append(
                SpendRow(
                    on.isoformat(),
                    tmpl.merchant,
                    tmpl.category,
                    tmpl.rupees,
                    tmpl.labels,
                    tmpl.account,
                )
            )
        for tmpl in _REFUND_TEMPLATE:
            if tmpl.slot != slot:
                continue
            on = _day_in_month(month_start.year, month_start.month, tmpl.day)
            if on > anchor:
                continue
            refunds.append(
                SpendRow(
                    on.isoformat(),
                    tmpl.merchant,
                    tmpl.category,
                    tmpl.rupees,
                    tmpl.labels,
                    tmpl.account,
                    _original(tmpl, anchor, months_back, window_months),
                )
            )
        for tmpl in _INCOME_TEMPLATE:
            if tmpl.slot != slot:
                continue
            on = _day_in_month(month_start.year, month_start.month, tmpl.day)
            if on > anchor:
                continue
            income.append(
                IncomeRow(
                    on.isoformat(),
                    tmpl.source,
                    tmpl.category,
                    tmpl.rupees,
                    tmpl.labels,
                    tmpl.account,
                )
            )
    return spends, refunds, income


def build_demo_transfers(
    anchor: date_t, *, window_months: int = DEMO_WINDOW_MONTHS
) -> list[TransferRow]:
    """The monthly bank → cash ATM withdrawals for the same window as
    :func:`build_demo_dataset`, minus any dated after ``anchor``. Both legs share one
    date, so the future-date filter can never keep one leg and drop the other."""
    transfers: list[TransferRow] = []
    for months_back in range(window_months - 1, -1, -1):
        month_start = _add_months(anchor, months_back)
        on = _day_in_month(month_start.year, month_start.month, _ATM_DAY)
        if on <= anchor:
            transfers.append(TransferRow(on.isoformat(), "bank", "cash", _ATM_RUPEES))
    return transfers


def build_demo_card_bills(
    anchor: date_t, *, window_months: int = DEMO_WINDOW_MONTHS
) -> list[CardBillRow]:
    """Each card's monthly bill for the window of :func:`build_demo_dataset`: paid from
    its ``parent`` on its ``bill_day`` for exactly the previous calendar month's net
    spend on it (purchases less refunds and cashback), minus any dated after ``anchor``.
    The window's oldest month has no bill: the month it would pay for is outside the
    window. Both legs share one date, so the future-date filter never splits a bill. The
    second card's latest bill is ``linked=False``: the one the demo leaves to link by hand.
    """
    spends, refunds, _income = build_demo_dataset(anchor, window_months=window_months)
    owed: dict[tuple[AccountKey, str], int] = {}
    for row in spends:
        owed[(row.account, row.date[:7])] = owed.get((row.account, row.date[:7]), 0) + row.rupees
    for row in refunds:
        owed[(row.account, row.date[:7])] = owed.get((row.account, row.date[:7]), 0) - row.rupees
    bills: list[CardBillRow] = []
    for months_back in range(window_months - 2, -1, -1):
        month_start = _add_months(anchor, months_back)
        billed_month = _add_months(anchor, months_back + 1).isoformat()[:7]
        for spec in ACCOUNTS:
            if spec.parent is None or spec.bill_day is None:
                continue
            on = _day_in_month(month_start.year, month_start.month, spec.bill_day)
            rupees = owed.get((spec.key, billed_month), 0)
            if on <= anchor and rupees > 0:
                bills.append(CardBillRow(on.isoformat(), spec.key, spec.parent, rupees))
    second = [i for i, bill in enumerate(bills) if bill.card == "card2"]
    if second:
        bills[second[-1]] = bills[second[-1]]._replace(linked=False)
    return bills


# Instruments + their transaction history: find-or-create rows
# (app.services.demo_seed._seed_investments), seeded once and never wiped, since
# holdings/XIRR need the full history since inception, not a recent slice. The dates
# below are a template: :func:`build_demo_instruments` moves them all by the same whole
# number of months so the newest lands as late as it can on or before the anchor, so
# an instrument created today (every guest sandbox) shows SIPs running up to now.
#
# ``amount``/``fee`` are native paise (cents for a USD instrument); ``units``/``price``
# are exact-decimal strings (parsed losslessly to Decimal). ``current_nav`` is set so
# holdings show value/P&L and the donut has slices. A couple of txns carry a ``note``
# (InvestmentTransaction also has the column).
#
# ``currency`` defaults to INR. A USD instrument's txns each carry ``fx``, the
# ``fx_rate_to_inr`` stamped on the row: a plausible rate for that date, written as a
# literal because the seeder is the server-side writer and fx_rates may be empty on a
# fresh box (ADR-0016 only forbids rewriting a stamp). The seed writes no ``fx_rates``
# row: that table is global, and a made-up rate there would stamp real users' rows. So
# until someone refreshes FX, the USD holdings show as FX-unavailable.
#
# The equities trade in more than one lot and sell part of a position, so FIFO
# (ADR-0015) books realised lots on both an INR and a USD instrument.
#
# ``isin`` is optional and only present where a price source needs it: it is the ONLY
# key AMFI NAVAll is matched on, so an ``indian_mf`` without one is a permanent
# refresh-navs dead-end ("no ISIN — cannot match AMFI NAVAll"). Equities need none —
# they price off symbol + exchange via Yahoo — and the hand-priced classes
# (fd / bond / nps / gold / other) have no auto source at all, by design.
def _monthly_sips(first: date_t, rupees: int, navs: tuple[str, ...]) -> list[dict[str, Any]]:
    """Monthly SIPs of ``rupees`` from ``first``, one per NAV in ``navs``. Units are
    the fund's allotment rounded to 3 decimals, as AMCs do, so ``amount`` is what was
    paid, not ``units * price``."""
    sips: list[dict[str, Any]] = []
    for i, nav in enumerate(navs):
        year, month0 = divmod(first.year * 12 + first.month - 1 + i, 12)
        units = (Decimal(rupees) / Decimal(nav)).quantize(Decimal("0.001"))
        sips.append(
            {
                "date": date_t(year, month0 + 1, first.day).isoformat(),
                "type": "sip",
                "units": str(units),
                "price": nav,
                "amount": rupees * 100,
            }
        )
    return sips


INSTRUMENTS: list[dict[str, Any]] = [
    {
        "symbol": "INFY",
        "name": "Infosys Ltd",
        "asset_class": "indian_equity",
        "exchange": "NSE",
        "current_nav": "1650",
        "txns": [
            {
                "date": "2025-08-12",
                "type": "buy",
                "units": "50",
                "price": "1400",
                "amount": 7000000,
                "fee": 5000,
                "note": "Opening position",
            },
            {"date": "2026-01-15", "type": "dividend", "amount": 50000},
            {
                "date": "2026-02-10",
                "type": "sell",
                "units": "10",
                "price": "1600",
                "amount": 1600000,
                "fee": 2000,
                "note": "Booked partial profit",
            },
        ],
    },
    {
        "symbol": "PPFCF",
        "name": "Parag Parikh Flexi Cap Fund",
        "asset_class": "indian_mf",
        "exchange": "MFCentral",
        # Real ISIN, so `refresh-navs` actually repositions this row off live AMFI data.
        # Direct-Plan-Growth (the Regular twin is INF879O01019) — a self-directed tracker's
        # default. AMFI lists both plans' IDCW variants under their own ISINs, so this pins
        # plan + option, not just the fund.
        "isin": "INF879O01027",
        "current_nav": "94.2",
        "txns": _monthly_sips(
            date_t(2025, 9, 5),
            5000,
            (
                "84.1",
                "85.3",
                "83.9",
                "86.2",
                "87.5",
                "86.8",
                "88.4",
                "89.9",
                "88.7",
                "90.6",
                "91.8",
                "90.9",
                "92.7",
                "93.5",
            ),
        ),
    },
    {
        "symbol": "UTINIFTY",
        "name": "UTI Nifty 50 Index Fund",
        "asset_class": "indian_mf",
        "exchange": "MFCentral",
        # Direct-Plan-Growth, checked against AMFI NAVAll (scheme code 120716).
        "isin": "INF789F01XA0",
        "current_nav": "178.19",
        "txns": _monthly_sips(
            date_t(2025, 9, 10),
            10000,
            (
                "158.4",
                "161.2",
                "163.9",
                "162.5",
                "160.8",
                "164.7",
                "166.3",
                "165.1",
                "168.9",
                "171.4",
                "170.2",
                "173.6",
                "175.8",
                "176.9",
            ),
        ),
    },
    {
        "symbol": "HDFCBANK",
        "name": "HDFC Bank Ltd",
        "asset_class": "indian_equity",
        "exchange": "NSE",
        "current_nav": "1005",
        "txns": [
            {
                "date": "2025-10-14",
                "type": "buy",
                "units": "40",
                "price": "960",
                "amount": 3840000,
                "fee": 2000,
            },
            {
                "date": "2026-01-20",
                "type": "buy",
                "units": "30",
                "price": "985",
                "amount": 2955000,
                "fee": 1500,
            },
            {
                "date": "2026-05-12",
                "type": "buy",
                "units": "30",
                "price": "1010",
                "amount": 3030000,
                "fee": 1500,
            },
            {"date": "2026-06-24", "type": "dividend", "amount": 220000},
            # FIFO: all of the first lot and 10 units of the second.
            {
                "date": "2026-08-18",
                "type": "sell",
                "units": "50",
                "price": "1045",
                "amount": 5225000,
                "fee": 2500,
                "note": "Trimmed after the results rally",
            },
        ],
    },
    {
        "symbol": "AAPL",
        "name": "Apple Inc",
        "asset_class": "us_equity",
        "exchange": "NASDAQ",
        "currency": "USD",
        "current_nav": "272.40",
        "txns": [
            {
                "date": "2025-10-06",
                "type": "buy",
                "units": "3",
                "price": "256.40",
                "amount": 76920,
                "fx": "88.72",
            },
            {
                "date": "2026-01-12",
                "type": "buy",
                "units": "3",
                "price": "261.10",
                "amount": 78330,
                "fx": "89.95",
            },
            {
                "date": "2026-04-13",
                "type": "buy",
                "units": "2",
                "price": "248.75",
                "amount": 49750,
                "fx": "88.40",
            },
            {
                "date": "2026-07-13",
                "type": "buy",
                "units": "2",
                "price": "268.30",
                "amount": 53660,
                "fx": "89.10",
            },
            {"date": "2026-08-14", "type": "dividend", "amount": 260, "fx": "89.30"},
            {
                "date": "2026-09-21",
                "type": "sell",
                "units": "4",
                "price": "275.00",
                "amount": 110000,
                "fx": "89.60",
            },
        ],
    },
    {
        "symbol": "VOO",
        "name": "Vanguard S&P 500 ETF",
        "asset_class": "us_etf",
        "exchange": "NYSE",
        "currency": "USD",
        "current_nav": "668.10",
        "txns": [
            {
                "date": "2025-11-10",
                "type": "buy",
                "units": "1",
                "price": "618.20",
                "amount": 61820,
                "fx": "88.65",
            },
            {
                "date": "2026-02-09",
                "type": "buy",
                "units": "1",
                "price": "630.50",
                "amount": 63050,
                "fx": "90.40",
            },
            {
                "date": "2026-05-11",
                "type": "buy",
                "units": "1",
                "price": "640.00",
                "amount": 64000,
                "fx": "89.00",
            },
            {
                "date": "2026-08-10",
                "type": "buy",
                "units": "1",
                "price": "655.40",
                "amount": 65540,
                "fx": "89.20",
            },
            {"date": "2026-09-29", "type": "dividend", "amount": 720, "fx": "89.50"},
        ],
    },
    {
        "symbol": "GOLDBEES",
        "name": "Nippon India Gold BeES",
        "asset_class": "gold",
        "exchange": "NSE",
        "current_nav": "68",
        "txns": [
            {
                "date": "2025-10-05",
                "type": "buy",
                "units": "80",
                "price": "62",
                "amount": 496000,
                "fee": 500,
            },
        ],
    },
    {
        "symbol": "QQQ",
        "name": "Invesco QQQ Trust",
        "asset_class": "us_etf",
        "exchange": "NASDAQ",
        "currency": "USD",
        "current_nav": "520.40",
        "txns": [
            {
                "date": "2025-11-17",
                "type": "buy",
                "units": "0.3125",
                "price": "480.00",
                "amount": 15000,
                "fx": "88.45",
            },
            {
                "date": "2026-02-16",
                "type": "buy",
                "units": "0.3960",
                "price": "505.00",
                "amount": 20000,
                "fx": "89.20",
            },
            {
                "date": "2026-05-18",
                "type": "buy",
                "units": "0.3883",
                "price": "515.00",
                "amount": 20000,
                "fx": "88.90",
            },
            {
                "date": "2026-08-17",
                "type": "buy",
                "units": "0.4780",
                "price": "523.00",
                "amount": 25000,
                "fx": "89.40",
            },
            {"date": "2026-09-22", "type": "dividend", "amount": 125, "fx": "89.50"},
        ],
    },
    {
        "symbol": "NVDA",
        "name": "NVIDIA Corp",
        "asset_class": "us_equity",
        "exchange": "NASDAQ",
        "currency": "USD",
        "current_nav": "142.50",
        "txns": [
            {
                "date": "2025-10-20",
                "type": "buy",
                "units": "2.4500",
                "price": "112.50",
                "amount": 27563,
                "fx": "88.60",
            },
            {
                "date": "2026-01-26",
                "type": "buy",
                "units": "1.8000",
                "price": "125.00",
                "amount": 22500,
                "fx": "89.80",
            },
            {
                "date": "2026-06-15",
                "type": "sell",
                "units": "1.2500",
                "price": "144.00",
                "amount": 18000,
                "fx": "89.10",
                "note": "Trimmed fractional lot",
            },
            {"date": "2026-07-02", "type": "dividend", "amount": 45, "fx": "89.25"},
        ],
    },
    {
        "symbol": "MSFT",
        "name": "Microsoft Corp",
        "asset_class": "us_equity",
        "exchange": "NASDAQ",
        "currency": "USD",
        "current_nav": "435.00",
        "txns": [
            {
                "date": "2025-11-24",
                "type": "buy",
                "units": "0.6500",
                "price": "415.00",
                "amount": 26975,
                "fx": "88.80",
            },
            {
                "date": "2026-03-23",
                "type": "buy",
                "units": "0.4500",
                "price": "428.00",
                "amount": 19260,
                "fx": "89.50",
            },
            {"date": "2026-06-11", "type": "dividend", "amount": 83, "fx": "89.05"},
        ],
    },
    {
        "symbol": "GOOGL",
        "name": "Alphabet Inc",
        "asset_class": "us_equity",
        "exchange": "NASDAQ",
        "currency": "USD",
        "current_nav": "188.00",
        "txns": [
            {
                "date": "2025-12-15",
                "type": "buy",
                "units": "1.5500",
                "price": "170.00",
                "amount": 26350,
                "fx": "89.15",
            },
            {
                "date": "2026-04-20",
                "type": "buy",
                "units": "0.9500",
                "price": "182.00",
                "amount": 17290,
                "fx": "88.75",
            },
        ],
    },
    {
        "symbol": "MIRAEELSS",
        "name": "Mirae Asset ELSS Tax Saver Fund",
        "asset_class": "indian_mf",
        "exchange": "MFCentral",
        # Direct-Plan-Growth (INF769K01DM9, AMFI 135781)
        "isin": "INF769K01DM9",
        "current_nav": "48.60",
        "txns": [
            {
                "date": "2025-12-20",
                "type": "buy",
                "units": "1028.807",
                "price": "43.74",
                "amount": 4500000,
                "note": "FY25-26 80C Tax Saver",
            },
        ],
    },
    {
        "symbol": "HDFC-FD-01",
        "name": "HDFC 1-Year Cumulative FD",
        "asset_class": "fd",
        "exchange": "OTHER",
        "currency": "INR",
        "current_nav": "100",
        "txns": [
            {
                "date": "2025-10-01",
                "type": "buy",
                "units": "2000",
                "price": "100",
                "amount": 20000000,
            },
            {"date": "2026-01-01", "type": "dividend", "amount": 362500},
            {"date": "2026-04-01", "type": "dividend", "amount": 362500},
            {"date": "2026-07-01", "type": "dividend", "amount": 362500},
        ],
    },
]


def _shift_months(on: date_t, months: int) -> date_t:
    year, month0 = divmod(on.year * 12 + on.month - 1 + months, 12)
    return _day_in_month(year, month0 + 1, on.day)


def build_demo_instruments(anchor: date_t) -> list[dict[str, Any]]:
    """:data:`INSTRUMENTS` with every txn date moved by one whole number of months, the
    most that keeps every date on or before ``anchor``. One shift for all keeps each
    instrument's order of trades, so FIFO (ADR-0015) books the same lots."""
    dates = [date_t.fromisoformat(t["date"]) for spec in INSTRUMENTS for t in spec["txns"]]
    newest = max(dates)
    months = (anchor.year * 12 + anchor.month) - (newest.year * 12 + newest.month)
    while any(_shift_months(d, months) > anchor for d in dates):
        months -= 1
    return [
        {
            **spec,
            "txns": [
                {**t, "date": _shift_months(date_t.fromisoformat(t["date"]), months).isoformat()}
                for t in spec["txns"]
            ],
        }
        for spec in INSTRUMENTS
    ]


def build_demo_sip_spends(anchor: date_t) -> list[SpendRow]:
    """Bank account debits on ``HDFC Savings`` for the mutual fund SIPs in
    :func:`build_demo_instruments`.

    Categorised under ``system:investment_transfer`` (INVESTMENT_TRANSFER_CATEGORY_NAME)
    and dated to match each fund's allotment date. In :mod:`app.services.demo_seed`,
    these are linked via ``linked_spend_txn_id`` to the corresponding SIP rows,
    with the newest UTINIFTY SIP left unlinked to showcase manual linking.
    """
    spends: list[SpendRow] = []
    instruments = build_demo_instruments(anchor)
    for inst in instruments:
        if inst["symbol"] == "PPFCF":
            for t in inst["txns"]:
                if t.get("type") == "sip":
                    spends.append(
                        SpendRow(
                            date=str(t["date"]),
                            merchant="BSE STAR MF - Parag Parikh Flexi Cap",
                            category="system:investment_transfer",
                            rupees=int(t["amount"]) // 100,
                            account="bank",
                        )
                    )
        elif inst["symbol"] == "UTINIFTY":
            for t in inst["txns"]:
                if t.get("type") == "sip":
                    spends.append(
                        SpendRow(
                            date=str(t["date"]),
                            merchant="ACH Debit - UTI Mutual Fund",
                            category="system:investment_transfer",
                            rupees=int(t["amount"]) // 100,
                            account="bank",
                        )
                    )
    return spends
