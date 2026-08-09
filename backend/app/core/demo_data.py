"""Demo dataset — single source of truth for the demo/sample data.

Pure data, no I/O. Two consumers share it so the demo set never drifts:

* :mod:`app.services.demo_seed` — writes it straight to the ORM in the app
  lifespan (a fresh DB self-seeds on boot; PRD §Users & access v2 demo account).
* :mod:`scripts.seed_dev_data` — drives the running HTTP API to (re)seed a
  server that's already up (or to add new months without a DB reset).

Rupee magnitudes here are whole ₹; each writer converts to paise (×100).
Investment ``amount``/``fee`` are already native paise; ``units``/``price`` are
exact-decimal strings.

Fingerprint stability (PRD §F4 = date + amount + normalized_merchant +
account_id): the 2026-04 / -05 / -06 spend + salary rows are PRESERVED VERBATIM
from the original 3-month seed, so an already-seeded DB 409-skips (HTTP writer)
them on re-run instead of duplicating. Adding new months is fine; never mutate an
existing tuple's date / merchant / amount.

``labels`` (F3a user tags) are populated on a representative subset of spends so
the tag feature — chips, autocomplete, and the board tag filter — is visibly
demonstrated without every row looking annotated. Empty tuple = no labels. (The
free-text ``note`` was dropped when labels landed; ``InvestmentTransaction`` keeps
its ``note``.)
"""

from __future__ import annotations

from typing import Any, NamedTuple


class SpendRow(NamedTuple):
    """A card spend / refund row. ``rupees`` is a positive magnitude; the writer
    signs it (spend negative, refund positive). ``labels`` are F3a user tags
    (get-or-created on seed)."""

    date: str
    merchant: str
    category: str
    rupees: int
    labels: tuple[str, ...] = ()


class IncomeRow(NamedTuple):
    """A bank income row. ``rupees`` is positive (credited to the bank)."""

    date: str
    source: str
    category: str
    rupees: int
    labels: tuple[str, ...] = ()


# Card spends across twelve months & categories, stored negative (spend) on the
# credit card. Spans 2025-08 → 2026-07 so the trailing-12 cashflow / category
# charts and the spend-over-time bar each see a full window. Merchants recur
# (Big Basket, Swiggy, Uber, Netflix, …) so the top-merchants aggregate ranks
# meaningfully; 2025-10 is a deliberate Diwali DEFICIT month (Travel + Shopping
# spike pushes spend above the 50k salary) so the cashflow net line dips below 0.
CARD_SPENDS: list[SpendRow] = [
    # 2025-08
    SpendRow("2025-08-05", "Big Basket", "Groceries", 2800),
    SpendRow("2025-08-10", "Swiggy", "Food", 650, ("online",)),
    SpendRow("2025-08-14", "Uber", "Transport", 380),
    SpendRow("2025-08-20", "Netflix", "Subscriptions", 649),
    SpendRow("2025-08-24", "Amazon", "Shopping", 1500, ("online",)),
    # 2025-09
    SpendRow("2025-09-06", "Big Basket", "Groceries", 3100),
    SpendRow("2025-09-11", "Zomato", "Food", 720, ("online",)),
    SpendRow("2025-09-15", "Metro", "Transport", 250),
    SpendRow("2025-09-20", "Netflix", "Subscriptions", 649),
    SpendRow("2025-09-21", "Spotify", "Subscriptions", 149),
    SpendRow("2025-09-26", "Apollo Pharmacy", "Health", 540),
    # 2025-10 — Diwali deficit month (spend > 50k salary → net < 0)
    SpendRow("2025-10-04", "Big Basket", "Groceries", 3600),
    SpendRow("2025-10-09", "Swiggy", "Food", 980, ("online",)),
    SpendRow("2025-10-12", "MakeMyTrip", "Travel", 32000, ("travel", "goa")),
    SpendRow("2025-10-18", "Croma", "Shopping", 18500, ("festive",)),
    SpendRow("2025-10-20", "Netflix", "Subscriptions", 649),
    SpendRow("2025-10-22", "Myntra", "Shopping", 6200, ("festive",)),
    SpendRow("2025-10-25", "Uber", "Transport", 720, ("travel",)),
    # 2025-11
    SpendRow("2025-11-05", "Big Basket", "Groceries", 2900),
    SpendRow("2025-11-09", "Blue Tokai", "Food", 480, ("restaurant",)),
    SpendRow("2025-11-14", "Swiggy", "Food", 610, ("online",)),
    SpendRow("2025-11-16", "Metro", "Transport", 300),
    SpendRow("2025-11-20", "Netflix", "Subscriptions", 649),
    SpendRow("2025-11-23", "BookMyShow", "Entertainment", 900, ("weekend",)),
    # 2025-12
    SpendRow("2025-12-06", "Big Basket", "Groceries", 3400),
    SpendRow("2025-12-11", "Zomato", "Food", 850, ("online",)),
    SpendRow("2025-12-15", "Uber", "Transport", 560),
    SpendRow("2025-12-19", "Amazon", "Shopping", 3200, ("online", "gifts")),
    SpendRow("2025-12-20", "Netflix", "Subscriptions", 649),
    SpendRow("2025-12-27", "Airtel", "Utilities", 1200),
    # 2026-01
    SpendRow("2026-01-05", "Big Basket", "Groceries", 3000),
    SpendRow("2026-01-10", "Swiggy", "Food", 700, ("online",)),
    SpendRow("2026-01-14", "Metro", "Transport", 280),
    SpendRow("2026-01-20", "Netflix", "Subscriptions", 649),
    SpendRow("2026-01-21", "Spotify", "Subscriptions", 149),
    SpendRow("2026-01-25", "Apollo Pharmacy", "Health", 620),
    # 2026-02
    SpendRow("2026-02-06", "Big Basket", "Groceries", 3300),
    SpendRow("2026-02-11", "Blue Tokai", "Food", 520, ("restaurant",)),
    SpendRow("2026-02-15", "Uber", "Transport", 410),
    SpendRow("2026-02-19", "Amazon", "Shopping", 2100, ("online",)),
    SpendRow("2026-02-20", "Netflix", "Subscriptions", 649),
    # 2026-03
    SpendRow("2026-03-05", "Big Basket", "Groceries", 3500),
    SpendRow("2026-03-10", "Zomato", "Food", 890, ("online",)),
    SpendRow("2026-03-14", "Metro", "Transport", 320),
    SpendRow("2026-03-18", "Myntra", "Shopping", 4200),
    SpendRow("2026-03-20", "Netflix", "Subscriptions", 649),
    SpendRow("2026-03-26", "Airtel", "Utilities", 1150),
    # 2026-04 — preserved verbatim (fingerprint-stable)
    SpendRow("2026-04-04", "Big Basket", "Groceries", 3200),
    SpendRow("2026-04-09", "Swiggy", "Food", 890, ("online",)),
    SpendRow("2026-04-15", "Uber", "Transport", 450),
    SpendRow("2026-04-22", "Amazon", "Shopping", 2400, ("online",)),
    SpendRow("2026-04-27", "Apollo Pharmacy", "Health", 780),
    # 2026-05 — preserved verbatim (fingerprint-stable)
    SpendRow("2026-05-03", "Blue Tokai", "Food", 520, ("restaurant",)),
    SpendRow("2026-05-08", "Metro", "Transport", 300),
    SpendRow("2026-05-14", "Big Basket", "Groceries", 4100),
    SpendRow("2026-05-20", "Netflix", "Subscriptions", 649),
    SpendRow("2026-05-25", "Croma", "Shopping", 1200),
    # 2026-06 — preserved verbatim (fingerprint-stable)
    SpendRow("2026-06-03", "Blue Tokai", "Food", 450, ("restaurant",)),
    SpendRow("2026-06-07", "Metro card", "Transport", 180),
    SpendRow("2026-06-09", "Big Basket", "Groceries", 1200),
    SpendRow("2026-06-12", "Swiggy", "Food", 895, ("online",)),
    # 2026-07 — current partial month
    SpendRow("2026-07-04", "Big Basket", "Groceries", 2600),
    SpendRow("2026-07-09", "Swiggy", "Food", 740, ("online",)),
    SpendRow("2026-07-14", "Uber", "Transport", 390),
    SpendRow("2026-07-16", "Netflix", "Subscriptions", 649),
]

# Card refunds: positive, same category as the spend they offset (PRD §F4a). The
# 2026-05-18 row is preserved verbatim; 2025-10-28 partially returns Diwali
# shopping (still leaves 2025-10 in deficit).
CARD_REFUNDS: list[SpendRow] = [
    SpendRow("2025-10-28", "Myntra refund", "Shopping", 2000, ("festive",)),
    SpendRow("2026-05-18", "Amazon refund", "Shopping", 600, ("online",)),
]

# Bank income: stored positive on the bank. Salary every month + a year-end and an
# appraisal bonus (Salary), two freelancing payouts, and card cashback so
# income-vs-spend isn't a flat band. The 2026-04 / -05 / -06 salary rows are
# preserved verbatim (fingerprint-stable).
BANK_INCOME: list[IncomeRow] = [
    IncomeRow("2025-08-01", "Acme Payroll", "Salary", 50000),
    IncomeRow("2025-09-01", "Acme Payroll", "Salary", 50000),
    IncomeRow("2025-09-18", "Upwork", "Freelancing", 12000),
    IncomeRow("2025-10-01", "Acme Payroll", "Salary", 50000),
    IncomeRow("2025-11-01", "Acme Payroll", "Salary", 50000),
    IncomeRow("2025-11-28", "Card Cashback", "Cashback", 350),
    IncomeRow("2025-12-01", "Acme Payroll", "Salary", 50000),
    IncomeRow("2025-12-15", "Acme Year-End Bonus", "Salary", 45000),
    IncomeRow("2026-01-01", "Acme Payroll", "Salary", 50000),
    IncomeRow("2026-01-30", "Card Cashback", "Cashback", 420),
    IncomeRow("2026-02-01", "Acme Payroll", "Salary", 50000),
    IncomeRow("2026-02-22", "Consulting", "Freelancing", 15000),
    IncomeRow("2026-03-01", "Acme Payroll", "Salary", 50000),
    IncomeRow("2026-03-20", "Acme Appraisal Bonus", "Salary", 35000),
    IncomeRow("2026-04-01", "Acme Payroll", "Salary", 50000),  # preserved verbatim
    IncomeRow("2026-05-01", "Acme Payroll", "Salary", 50000),  # preserved verbatim
    IncomeRow("2026-06-01", "Acme Payroll", "Salary", 50000),  # preserved verbatim
    IncomeRow("2026-07-01", "Acme Payroll", "Salary", 50000),
]

# Instruments + their transaction history. ``amount``/``fee`` are native paise;
# ``units``/``price`` are exact-decimal strings (parsed losslessly to Decimal).
# ``current_nav`` is set so holdings show value/P&L and the donut has slices.
# A couple of txns carry a ``note`` (InvestmentTransaction also has the column).
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
