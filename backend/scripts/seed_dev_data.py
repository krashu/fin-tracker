"""Seed the local dev database with demo data for the dashboards and frontend.

NOTE: a fresh DB now self-seeds on boot (SEED_DEMO_ON_STARTUP; see
app.services.demo_seed), so `make backend` already comes up populated. This
script stays useful for reseeding a server that's already running (e.g. after
adding new months to app.core.demo_data) without a restart, and for driving the
data through the real API validation path.

Run this against a running backend to populate the dev DB so every page —
Overview, Spending, Expenses, Holdings, Transactions, Portfolio — shows realistic
content instead of empty states. It is **self-contained**: it logs in as the
demo account (public creds seeded by migration 0017) and drives plain API calls —
no statement fixture required, and fully deterministic.

What it creates (all idempotent — find-or-create for accounts/instruments, a
409-skip for transactions — so re-runs add nothing and never error):

  - **Accounts** — an HDFC Savings bank account and an Axis Flipkart credit card.
  - **Spending** — ~12 months (2025-08 → 2026-07) of categorized card spends +
    refunds, and monthly salary income (plus two bonuses, freelancing, and
    cashback) on the bank. One deliberate Diwali deficit month (2025-10) pushes
    spend above salary so the cashflow net line dips below zero. Drives the
    net-worth Assets line (bank cash), the card's year-to-date spend figure,
    the month's spend/income figures, and the category / cashflow / trend /
    top-merchant charts. Note the card balance is NOT an "Owed" line: credit
    cards are excluded from net worth entirely (spend channel, not a
    liability), so with these two accounts the Owed line never renders.
  - **Investments** — three priced instruments (equity / mutual fund / gold) with
    buy / SIP / sell / dividend history. Drives the Holdings table (units, value,
    P&L, %alloc, XIRR), the Portfolio summary tiles, and the allocation donut.

It drives the RUNNING API (localhost:8000) rather than writing the DB directly,
which avoids SQLite write-lock contention with the live uvicorn process and means
the demo data goes through the same validation as real input.

Prerequisites: the backend is running (`make backend`, in a separate terminal).

Run:  cd backend && uv run python scripts/seed_dev_data.py

For a pristine demo, reset first (this script never destroys data): stop the
backend, delete the dev SQLite DB, `uv run alembic upgrade head`, restart the
backend, then run this.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

import httpx

from app.core.demo import DEMO_EMAIL, DEMO_PASSWORD
from app.core.demo_data import BANK_INCOME, CARD_REFUNDS, CARD_SPENDS, INSTRUMENTS

# 127.0.0.1, not localhost: on Windows "localhost" can resolve to IPv6 ::1 first,
# but uvicorn binds IPv4 127.0.0.1 — httpx (unlike the browser) won't fall back.
API = "http://127.0.0.1:8000/api/v1"

# The API is now authenticated (PRD §Users & access v2). The seeder logs in as
# the demo account and drives its data. ORIGIN must be an allowed CORS origin so
# the fail-closed Origin CSRF check lets the POSTs through.
ORIGIN = "http://localhost:3000"

# --------------------------------------------------------------------------- #
# Demo data lives in app.core.demo_data (shared with the in-process startup
# seeder, app.services.demo_seed, so the two never drift). Rupee magnitudes are
# converted to paise at post time (×100); investment amounts/fees are already in
# native paise.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Idempotent helpers
# --------------------------------------------------------------------------- #
def _find_or_create_account(
    c: httpx.Client, *, match: Callable[[dict], bool], body: dict
) -> tuple[dict, bool]:
    """Return ``(account, created)``; reuse the first existing account matching
    ``match`` so re-runs don't pile up duplicates."""
    existing = next((a for a in c.get("/accounts").raise_for_status().json() if match(a)), None)
    if existing is not None:
        return existing, False
    return c.post("/accounts", json=body).raise_for_status().json(), True


def _find_or_create_instrument(c: httpx.Client, *, symbol: str, body: dict) -> tuple[dict, bool]:
    """Return ``(instrument, created)``, matched by symbol."""
    existing = next(
        (i for i in c.get("/instruments").raise_for_status().json() if i["symbol"] == symbol),
        None,
    )
    if existing is not None:
        return existing, False
    return c.post("/instruments", json=body).raise_for_status().json(), True


def _post_transaction(c: httpx.Client, body: dict) -> bool:
    """POST a manual transaction; a 409 (duplicate fingerprint, PRD §F4 =
    date+amount+merchant+account) means it was already seeded, so we skip it.
    Returns True only when a new row is created."""
    r = c.post("/transactions", json=body)
    if r.status_code == 409:
        return False
    r.raise_for_status()
    return True


def _categories_by_kind(c: httpx.Client) -> tuple[dict[str, int], dict[str, int]]:
    """(spend_by_name, income_by_name) — lowercase name → id, from the seeded set."""
    cats = c.get("/categories").raise_for_status().json()
    spend = {x["name"].lower(): x["id"] for x in cats if x["kind"] == "spend"}
    income = {x["name"].lower(): x["id"] for x in cats if x["kind"] == "income"}
    return spend, income


# --------------------------------------------------------------------------- #
# Seed steps
# --------------------------------------------------------------------------- #
def seed_accounts(c: httpx.Client) -> tuple[dict, dict]:
    """Find-or-create the demo credit card and bank account."""
    cc, made_cc = _find_or_create_account(
        c,
        match=lambda a: (
            a["type"] == "credit_card"
            and a.get("issuer") == "axis"
            and a.get("archived_at") is None
        ),
        body={
            "name": "Axis Flipkart",
            "type": "credit_card",
            "issuer": "axis",
            # Placeholder last-4 — the demo account is a public "Try the demo"
            # login (PRD §Users & access v2). NEVER replace with a real card's
            # last-4; the demo DB must carry only synthetic identifiers.
            "last4": "4321",
            "currency": "INR",
            "opening_balance_paise": 0,
        },
    )
    bank, made_bank = _find_or_create_account(
        c,
        # Match by NAME (not first-by-type): account_id is part of the PRD §F4
        # fingerprint and /accounts is name-sorted, so first-by-type could shift
        # the target account when accounts are added — reseeding would then
        # duplicate rows under a different account. Name-matching pins the target.
        match=lambda a: a["name"] == "HDFC Savings" and a.get("archived_at") is None,
        body={
            "name": "HDFC Savings",
            "type": "bank",
            "issuer": "hdfc",
            "currency": "INR",
            "opening_balance_paise": 0,
        },
    )
    print(
        f"accounts: card id={cc['id']} ({'created' if made_cc else 'existing'}), "
        f"bank id={bank['id']} ({'created' if made_bank else 'existing'})"
    )
    return cc, bank


def seed_spending(c: httpx.Client, cc: dict, bank: dict) -> None:
    """Card spends + refund + bank salary income, mapped to seeded categories."""
    spend_cats, income_cats = _categories_by_kind(c)
    other_spend = spend_cats.get("other")
    other_income = income_cats.get("other")
    added = 0

    for row in CARD_SPENDS:
        added += _post_transaction(
            c,
            {
                "date": row.date,
                "account_id": cc["id"],
                "amount_paise": -row.rupees * 100,  # spend = negative
                "transaction_type": "spend",
                "merchant_raw": row.merchant,
                "category_id": spend_cats.get(row.category.lower(), other_spend),
                "labels": list(row.labels),
            },
        )
    for row in CARD_REFUNDS:
        added += _post_transaction(
            c,
            {
                "date": row.date,
                "account_id": cc["id"],
                "amount_paise": row.rupees * 100,  # refund = positive, same category
                "transaction_type": "refund",
                "merchant_raw": row.merchant,
                "category_id": spend_cats.get(row.category.lower(), other_spend),
                "labels": list(row.labels),
            },
        )
    for inc in BANK_INCOME:
        added += _post_transaction(
            c,
            {
                "date": inc.date,
                "account_id": bank["id"],
                "amount_paise": inc.rupees * 100,  # income = positive on the bank
                "transaction_type": "income",
                "merchant_raw": inc.source,
                "category_id": income_cats.get(inc.category.lower(), other_income),
                "labels": list(inc.labels),
            },
        )

    total = len(CARD_SPENDS) + len(CARD_REFUNDS) + len(BANK_INCOME)
    print(f"spending: {added} rows created, {total - added} already present")


def seed_investments(c: httpx.Client) -> None:
    """Find-or-create each instrument; seed its transactions only on creation
    (manual investment txns have no dedup, so gate on the instrument being new)."""
    made, skipped = 0, 0
    for spec in INSTRUMENTS:
        instrument, created = _find_or_create_instrument(
            c,
            symbol=spec["symbol"],
            body={
                "symbol": spec["symbol"],
                "name": spec["name"],
                "asset_class": spec["asset_class"],
                "exchange": spec["exchange"],
                "currency": "INR",
                # Only the MF carries one; omitting it left that holding permanently
                # unmatchable by AMFI (see the ``isin`` note in app.core.demo_data).
                "isin": spec.get("isin"),
                "current_nav": spec["current_nav"],
            },
        )
        if not created:
            skipped += 1
            continue
        for t in spec["txns"]:
            body: dict = {
                "date": t["date"],
                "instrument_id": instrument["id"],
                "transaction_type": t["type"],
                "amount_native_paise": t.get("amount", 0),
                "fees_native_paise": t.get("fee", 0),
            }
            if "units" in t:
                body["units"] = t["units"]
            if "price" in t:
                body["price_per_unit_native"] = t["price"]
            if t.get("note"):
                body["note"] = t["note"]
            c.post("/investment-transactions", json=body).raise_for_status()
        made += 1
    print(f"investments: {made} instruments seeded, {skipped} already present")


def seed_benchmarks(c: httpx.Client) -> None:
    """Backfill benchmark NAV history from mfapi so the portfolio-vs-benchmark view has
    data. The catalog rows themselves are migration-seeded; this only fills the NAV cache
    (a one-shot cold trigger, never the hot path). Resilient: if mfapi is unreachable the
    endpoint still returns 200 with the failures counted, so the seed never aborts here."""
    summary = c.post("/benchmarks/refresh").raise_for_status().json()
    print(
        f"benchmarks: {summary['benchmarks_refreshed']} refreshed, "
        f"{summary['navs_inserted']} NAV rows cached, {summary['fetch_errors']} fetch errors"
    )


def main() -> int:
    # Default Origin header on every request → satisfies the CSRF Origin check.
    with httpx.Client(base_url=API, timeout=90.0, headers={"origin": ORIGIN}) as c:
        try:
            # Authenticate as the demo user (seeded by migration 0017). The login
            # sets the httpOnly access cookie on the client jar, which httpx then
            # sends on every subsequent request — so all seed calls run as demo.
            c.post(
                "/auth/login",
                json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
            ).raise_for_status()
        except httpx.HTTPError as exc:
            print(
                f"backend not reachable / demo login failed at {API} — is it running "
                f"and migrated (alembic upgrade head seeds the demo user)? A 401 here "
                f"means the demo login is gated off: this seeder authenticates as the "
                f"demo account, which needs DEMO_LOGIN_ENABLED=true in .env (off by "
                f"default — the password is public). ({exc})",
                file=sys.stderr,
            )
            return 1
        cc, bank = seed_accounts(c)
        seed_spending(c, cc, bank)
        seed_investments(c)
        seed_benchmarks(c)
    print("done — open http://localhost:3000 to see the populated dashboards.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
