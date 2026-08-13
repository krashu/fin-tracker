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

The merchant vocabulary is deliberately UNCHANGED from the old fixed dataset —
only which relative month/day each row lands on moved. ``tests/test_migration_parity.py``
hardcodes the 12 merchants this dataset teaches (F3 auto-tag learning) as
colliding with the ADR-0011 seed dictionary; renaming or dropping one of those
12 breaks that test. Add new merchants freely; don't rename the existing ones.

The current (slot 0) month is truncated to ``day <= anchor.day`` so a demo
session never shows a future-dated transaction.
"""

from __future__ import annotations

import calendar
from datetime import date as date_t
from typing import Any, Literal, NamedTuple

# Trailing window the boot-time seeder keeps in place, in months (inclusive of
# the current partial month). Picked as a bit more than a year so the rolling
# window always has full-year context, not just a bare 12.
DEMO_WINDOW_MONTHS = 14


class SpendRow(NamedTuple):
    """A concrete, dated card spend / refund row. ``rupees`` is a positive
    magnitude; the writer signs it (spend negative, refund positive). ``labels``
    are F3a user tags (get-or-created on seed)."""

    date: str
    merchant: str
    category: str
    rupees: int
    labels: tuple[str, ...] = ()


class IncomeRow(NamedTuple):
    """A concrete, dated income row. ``rupees`` is positive. ``account`` picks
    which demo account it posts to — almost everything is bank-credited salary,
    but card cashback is credited to the CARD statement, not the linked bank
    account (real Axis Flipkart behaviour)."""

    date: str
    source: str
    category: str
    rupees: int
    labels: tuple[str, ...] = ()
    account: Literal["bank", "card"] = "bank"


class _SpendTemplate(NamedTuple):
    """One recurring spend/refund slot. ``slot`` counts months back from the
    anchor's month (0 = current month, 11 = oldest of the core cycle)."""

    slot: int
    day: int
    merchant: str
    category: str
    rupees: int
    labels: tuple[str, ...] = ()


class _IncomeTemplate(NamedTuple):
    slot: int
    day: int
    source: str
    category: str
    rupees: int
    labels: tuple[str, ...] = ()
    account: Literal["bank", "card"] = "bank"


# Card spends across the 12-slot cycle. Merchants recur (Big Basket, Swiggy,
# Uber, Netflix, …) so the top-merchants aggregate ranks meaningfully; slot 9
# is a deliberate deficit slot (Travel + Shopping spike pushes spend above the
# 50k salary) so the cashflow net line dips below 0 for that month.
_SPEND_TEMPLATE: list[_SpendTemplate] = [
    # slot 11 (oldest)
    _SpendTemplate(11, 5, "Big Basket", "Groceries", 2800),
    _SpendTemplate(11, 10, "Swiggy", "Online Food Delivery", 650, ("online",)),
    _SpendTemplate(11, 14, "Uber", "Ride-Hailing & Taxis", 380),
    _SpendTemplate(11, 20, "Netflix", "Digital Subscriptions & Streaming", 649),
    _SpendTemplate(11, 24, "Amazon", "Shopping", 1500, ("online",)),
    # slot 10
    _SpendTemplate(10, 6, "Big Basket", "Groceries", 3100),
    _SpendTemplate(10, 11, "Zomato", "Online Food Delivery", 720, ("online",)),
    _SpendTemplate(10, 15, "Metro", "Metro & Public Transit", 250),
    _SpendTemplate(10, 20, "Netflix", "Digital Subscriptions & Streaming", 649),
    _SpendTemplate(10, 21, "Spotify", "Digital Subscriptions & Streaming", 149),
    _SpendTemplate(10, 26, "Apollo Pharmacy", "Health", 540),
    # slot 9 — deficit slot (spend > 50k salary → net < 0)
    _SpendTemplate(9, 4, "Big Basket", "Groceries", 3600),
    _SpendTemplate(9, 9, "Swiggy", "Online Food Delivery", 980, ("online",)),
    _SpendTemplate(9, 12, "MakeMyTrip", "Travel", 32000, ("travel", "goa")),
    _SpendTemplate(9, 18, "Croma", "Electronics & Gadgets", 18500, ("festive",)),
    _SpendTemplate(9, 20, "Netflix", "Digital Subscriptions & Streaming", 649),
    _SpendTemplate(9, 22, "Myntra", "Clothing & Apparel", 6200, ("festive",)),
    _SpendTemplate(9, 25, "Uber", "Ride-Hailing & Taxis", 720, ("travel",)),
    # slot 8
    _SpendTemplate(8, 5, "Big Basket", "Groceries", 2900),
    _SpendTemplate(8, 9, "Blue Tokai", "Coffee & Tea", 480, ("restaurant",)),
    _SpendTemplate(8, 14, "Swiggy", "Online Food Delivery", 610, ("online",)),
    _SpendTemplate(8, 16, "Metro", "Metro & Public Transit", 300),
    _SpendTemplate(8, 20, "Netflix", "Digital Subscriptions & Streaming", 649),
    _SpendTemplate(8, 23, "BookMyShow", "Entertainment", 900, ("weekend",)),
    # slot 7
    _SpendTemplate(7, 6, "Big Basket", "Groceries", 3400),
    _SpendTemplate(7, 11, "Zomato", "Online Food Delivery", 850, ("online",)),
    _SpendTemplate(7, 15, "Uber", "Ride-Hailing & Taxis", 560),
    _SpendTemplate(7, 19, "Amazon", "Shopping", 3200, ("online", "gifts")),
    _SpendTemplate(7, 20, "Netflix", "Digital Subscriptions & Streaming", 649),
    _SpendTemplate(7, 27, "Airtel", "Mobile & Broadband", 1200),
    # slot 6
    _SpendTemplate(6, 5, "Big Basket", "Groceries", 3000),
    _SpendTemplate(6, 10, "Swiggy", "Online Food Delivery", 700, ("online",)),
    _SpendTemplate(6, 14, "Metro", "Metro & Public Transit", 280),
    _SpendTemplate(6, 20, "Netflix", "Digital Subscriptions & Streaming", 649),
    _SpendTemplate(6, 21, "Spotify", "Digital Subscriptions & Streaming", 149),
    _SpendTemplate(6, 25, "Apollo Pharmacy", "Health", 620),
    # slot 5
    _SpendTemplate(5, 6, "Big Basket", "Groceries", 3300),
    _SpendTemplate(5, 11, "Blue Tokai", "Coffee & Tea", 520, ("restaurant",)),
    _SpendTemplate(5, 15, "Uber", "Ride-Hailing & Taxis", 410),
    _SpendTemplate(5, 19, "Amazon", "Shopping", 2100, ("online",)),
    _SpendTemplate(5, 20, "Netflix", "Digital Subscriptions & Streaming", 649),
    # slot 4
    _SpendTemplate(4, 5, "Big Basket", "Groceries", 3500),
    _SpendTemplate(4, 10, "Zomato", "Online Food Delivery", 890, ("online",)),
    _SpendTemplate(4, 14, "Metro", "Metro & Public Transit", 320),
    _SpendTemplate(4, 18, "Myntra", "Clothing & Apparel", 4200),
    _SpendTemplate(4, 20, "Netflix", "Digital Subscriptions & Streaming", 649),
    _SpendTemplate(4, 26, "Airtel", "Mobile & Broadband", 1150),
    # slot 3
    _SpendTemplate(3, 4, "Big Basket", "Groceries", 3200),
    _SpendTemplate(3, 9, "Swiggy", "Online Food Delivery", 890, ("online",)),
    _SpendTemplate(3, 15, "Uber", "Ride-Hailing & Taxis", 450),
    _SpendTemplate(3, 22, "Amazon", "Shopping", 2400, ("online",)),
    _SpendTemplate(3, 27, "Apollo Pharmacy", "Health", 780),
    # slot 2
    _SpendTemplate(2, 3, "Blue Tokai", "Coffee & Tea", 520, ("restaurant",)),
    _SpendTemplate(2, 8, "Metro", "Metro & Public Transit", 300),
    _SpendTemplate(2, 14, "Big Basket", "Groceries", 4100),
    _SpendTemplate(2, 20, "Netflix", "Digital Subscriptions & Streaming", 649),
    _SpendTemplate(2, 25, "Croma", "Electronics & Gadgets", 1200),
    # slot 1
    _SpendTemplate(1, 3, "Blue Tokai", "Coffee & Tea", 450, ("restaurant",)),
    _SpendTemplate(1, 7, "Metro card", "Metro & Public Transit", 180),
    _SpendTemplate(1, 9, "Big Basket", "Groceries", 1200),
    _SpendTemplate(1, 12, "Swiggy", "Online Food Delivery", 895, ("online",)),
    # slot 0 (current month) — truncated to day <= anchor.day at generation time
    _SpendTemplate(0, 4, "Big Basket", "Groceries", 2600),
    _SpendTemplate(0, 9, "Swiggy", "Online Food Delivery", 740, ("online",)),
    _SpendTemplate(0, 14, "Uber", "Ride-Hailing & Taxis", 390),
    _SpendTemplate(0, 16, "Netflix", "Digital Subscriptions & Streaming", 649),
]

# Card refunds: `spend`-typed with a POSITIVE amount (ADR-0009 — a refund is not
# its own type), same category as the spend they offset so the §F4a signed sum
# nets. Slot 9's refund partially returns that slot's deficit-driving shopping
# spike (still leaves slot 9 in deficit); slot 2's returns an Amazon order.
_REFUND_TEMPLATE: list[_SpendTemplate] = [
    _SpendTemplate(9, 28, "Myntra refund", "Clothing & Apparel", 2000, ("festive",)),
    _SpendTemplate(2, 18, "Amazon refund", "Shopping", 600, ("online",)),
]

# Bank/card income: salary every slot + a year-end and an appraisal bonus
# (Salary), two freelancing payouts, and card cashback so income-vs-spend isn't
# a flat band. Cashback posts to the CARD (real Axis Flipkart behaviour — it's
# credited on the card statement, not the linked bank account).
_INCOME_TEMPLATE: list[_IncomeTemplate] = [
    _IncomeTemplate(11, 1, "Acme Payroll", "Salary", 50000),
    _IncomeTemplate(10, 1, "Acme Payroll", "Salary", 50000),
    _IncomeTemplate(10, 18, "Upwork", "Freelancing", 12000),
    _IncomeTemplate(9, 1, "Acme Payroll", "Salary", 50000),
    _IncomeTemplate(8, 1, "Acme Payroll", "Salary", 50000),
    _IncomeTemplate(8, 28, "Card Cashback", "Cashback", 350, (), "card"),
    _IncomeTemplate(7, 1, "Acme Payroll", "Salary", 50000),
    _IncomeTemplate(7, 15, "Acme Year-End Bonus", "Salary", 45000),
    _IncomeTemplate(6, 1, "Acme Payroll", "Salary", 50000),
    _IncomeTemplate(6, 30, "Card Cashback", "Cashback", 420, (), "card"),
    _IncomeTemplate(5, 1, "Acme Payroll", "Salary", 50000),
    _IncomeTemplate(5, 22, "Consulting", "Freelancing", 15000),
    _IncomeTemplate(4, 1, "Acme Payroll", "Salary", 50000),
    _IncomeTemplate(4, 20, "Acme Appraisal Bonus", "Salary", 35000),
    _IncomeTemplate(3, 1, "Acme Payroll", "Salary", 50000),
    _IncomeTemplate(2, 1, "Acme Payroll", "Salary", 50000),
    _IncomeTemplate(1, 1, "Acme Payroll", "Salary", 50000),
    _IncomeTemplate(0, 1, "Acme Payroll", "Salary", 50000),
]


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
        for tmpl in _SPEND_TEMPLATE:
            if tmpl.slot != slot:
                continue
            on = _day_in_month(month_start.year, month_start.month, tmpl.day)
            if on > anchor:
                continue
            spends.append(
                SpendRow(on.isoformat(), tmpl.merchant, tmpl.category, tmpl.rupees, tmpl.labels)
            )
        for tmpl in _REFUND_TEMPLATE:
            if tmpl.slot != slot:
                continue
            on = _day_in_month(month_start.year, month_start.month, tmpl.day)
            if on > anchor:
                continue
            refunds.append(
                SpendRow(on.isoformat(), tmpl.merchant, tmpl.category, tmpl.rupees, tmpl.labels)
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


# Instruments + their transaction history. Unaffected by the rolling window —
# holdings/XIRR need the full purchase history since inception, not a recent
# slice, so these stay fixed find-or-create rows (app.services.demo_seed
# ._seed_investments), seeded once and never wiped.
#
# ``amount``/``fee`` are native paise; ``units``/``price`` are exact-decimal
# strings (parsed losslessly to Decimal). ``current_nav`` is set so holdings
# show value/P&L and the donut has slices. A couple of txns carry a ``note``
# (InvestmentTransaction also has the column).
#
# ``isin`` is optional and only present where a price source needs it: it is the ONLY
# key AMFI NAVAll is matched on, so an ``indian_mf`` without one is a permanent
# refresh-navs dead-end ("no ISIN — cannot match AMFI NAVAll"). Equities need none —
# they price off symbol + exchange via Yahoo — and the hand-priced classes
# (fd / bond / nps / gold / other) have no auto source at all, by design.
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
        "current_nav": "78",
        "txns": [
            {"date": "2025-09-01", "type": "sip", "units": "100", "price": "65", "amount": 650000},
            {"date": "2025-12-01", "type": "sip", "units": "100", "price": "70", "amount": 700000},
            {"date": "2026-03-01", "type": "sip", "units": "100", "price": "72", "amount": 720000},
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
]
