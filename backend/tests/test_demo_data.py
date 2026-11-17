"""Unit tests for the pure demo-dataset generator (app.core.demo_data).

``build_demo_dataset`` has no DB/ORM dependency, so these test it directly
rather than through the seeder — in particular the current-month truncation
that keeps a synced demo account from ever showing a future-dated transaction,
which is easy to leave unexercised when every other test picks a mid-month
anchor.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.core.demo_data import (
    ACCOUNTS,
    DEMO_WINDOW_MONTHS,
    INSTRUMENTS,
    build_demo_card_bills,
    build_demo_dataset,
    build_demo_instruments,
    build_demo_transfers,
)
from app.services.import_service import PARSERS


def test_no_row_is_dated_after_the_anchor() -> None:
    """Early in the month is exactly where the current-month slot's later
    template days would otherwise land in the future."""
    anchor = date(2026, 8, 3)
    spends, refunds, income = build_demo_dataset(anchor)

    all_dates = [row.date for row in (*spends, *refunds, *income)]
    assert all_dates  # sanity: the window isn't empty
    assert all(date.fromisoformat(d) <= anchor for d in all_dates)


def test_current_month_still_yields_its_early_days_rows() -> None:
    """Truncation must not silently drop the WHOLE current month — only the
    days after the anchor."""
    anchor = date(2026, 8, 3)
    spends, _refunds, _income = build_demo_dataset(anchor)

    assert any(row.date == "2026-08-04" for row in spends) is False
    # slot 0's earliest spend template day is 4 (see _SPEND_TEMPLATE); an
    # anchor on day 3 truncates it out, but a later anchor the same month
    # includes it.
    spends_later, _, _ = build_demo_dataset(date(2026, 8, 10))
    assert any(row.date == "2026-08-04" for row in spends_later)


def test_window_narrows_to_requested_month_count() -> None:
    """A one-month window returns only the (truncated) current month."""
    anchor = date(2026, 8, 20)
    spends, refunds, income = build_demo_dataset(anchor, window_months=1)

    months = {d[:7] for row in (*spends, *refunds, *income) for d in [row.date]}
    assert months == {"2026-08"}


def test_default_window_spans_the_documented_month_count() -> None:
    anchor = date(2026, 8, 20)
    spends, refunds, income = build_demo_dataset(anchor)

    months = {d[:7] for row in (*spends, *refunds, *income) for d in [row.date]}
    # The oldest slot in the cycle repeats past 12 months, but the SET of
    # distinct calendar months touched must equal the configured window.
    assert len(months) == DEMO_WINDOW_MONTHS


def _monthly_net(anchor: date) -> dict[str, int]:
    spends, refunds, income = build_demo_dataset(anchor)
    net: dict[str, int] = {}
    for row in spends:
        net[row.date[:7]] = net.get(row.date[:7], 0) - row.rupees
    for row in (*refunds, *income):
        net[row.date[:7]] = net.get(row.date[:7], 0) + row.rupees
    return net


def _months_back(anchor: date, n: int) -> str:
    """``YYYY-MM`` of the month ``n`` months before ``anchor``'s."""
    year, month0 = divmod(anchor.year * 12 + anchor.month - 1 - n, 12)
    return f"{year:04d}-{month0 + 1:02d}"


def _year_of_anchors() -> list[date]:
    """Weekly anchors over a year: calendar-month extras (an annual premium, Diwali)
    move against the 12 slots as the window rolls, so one anchor proves little."""
    return [date(2026, 1, 1) + timedelta(days=offset) for offset in range(0, 365, 7)]


def test_only_the_deficit_slot_month_nets_negative() -> None:
    """The household budget: salary covers the steady month, and only slot 9 (a
    travel + shopping spike, nine months back) dips below zero."""
    for anchor in _year_of_anchors():
        negative = [month for month, net in _monthly_net(anchor).items() if net < 0]
        assert negative == [_months_back(anchor, 9)], anchor


@pytest.mark.parametrize("account", ["bank", "cash"])
def test_balance_never_goes_negative(account: str) -> None:
    """Nothing seeded overdraws the bank or the cash wallet, card bills included; net
    worth counts both signed."""
    for anchor in _year_of_anchors():
        spends, refunds, income = build_demo_dataset(anchor)
        transfers = build_demo_transfers(anchor)
        bills = build_demo_card_bills(anchor) if account == "bank" else []
        moves = sorted(
            [(r.date, -r.rupees) for r in spends if r.account == account]
            + [(r.date, r.rupees) for r in (*refunds, *income) if r.account == account]
            + [(t.date, -t.rupees) for t in transfers if t.source == account]
            + [(t.date, t.rupees) for t in transfers if t.dest == account]
            + [(b.date, -b.rupees) for b in bills],
            # Same-day credits first: the 1st's ATM withdrawal pays that day's maid.
            key=lambda move: (move[0], -move[1]),
        )
        balance = 0
        for on, rupees in moves:
            balance += rupees
            assert balance >= 0, (anchor, on, balance)


def test_transfers_run_monthly_from_bank_to_cash() -> None:
    anchor = date(2026, 8, 20)
    transfers = build_demo_transfers(anchor)
    assert len(transfers) == DEMO_WINDOW_MONTHS
    assert {(t.source, t.dest) for t in transfers} == {("bank", "cash")}
    assert all(date.fromisoformat(t.date) <= anchor for t in transfers)


def test_every_full_month_has_salary_rent_and_emi() -> None:
    anchor = date(2026, 8, 20)
    spends, _refunds, income = build_demo_dataset(anchor)
    full_months = {row.date[:7] for row in spends} - {anchor.isoformat()[:7]}
    assert {row.date[:7] for row in income if row.source == "Acme Payroll"} >= full_months
    for merchant in ("Rent - NoBroker", "HDFC Car Loan EMI"):
        assert {row.date[:7] for row in spends if row.merchant == merchant} >= full_months


def test_every_account_gets_rows() -> None:
    spends, refunds, income = build_demo_dataset(date(2026, 8, 20))
    used = {row.account for row in (*spends, *refunds, *income)}
    assert used == {spec.key for spec in ACCOUNTS}


def test_dataset_size_is_a_lived_in_year() -> None:
    """Enough rows to look lived-in, few enough that the guest sandbox (seeded inside
    its creating request) stays quick."""
    spends, refunds, income = build_demo_dataset(date(2026, 8, 20))
    assert 300 <= len(spends) + len(refunds) + len(income) <= 400


def test_each_card_bill_pays_the_previous_month_in_full() -> None:
    anchor = date(2026, 8, 20)
    spends, refunds, _income = build_demo_dataset(anchor)
    bills = build_demo_card_bills(anchor)
    assert {b.card for b in bills} == {s.key for s in ACCOUNTS if s.parent is not None}
    for bill in bills:
        on = date.fromisoformat(bill.date)
        assert on <= anchor
        billed = (on.replace(day=1) - timedelta(days=1)).isoformat()[:7]
        owed = sum(r.rupees for r in spends if r.account == bill.card and r.date[:7] == billed)
        owed -= sum(r.rupees for r in refunds if r.account == bill.card and r.date[:7] == billed)
        assert bill.rupees == owed


def test_every_month_but_the_oldest_bills_each_card() -> None:
    """At an anchor past both bill days, every month after the window's first pays
    both cards; the oldest month's bill would pay for a month outside the window."""
    anchor = date(2026, 8, 25)
    bills = build_demo_card_bills(anchor)
    assert len(bills) == 2 * (DEMO_WINDOW_MONTHS - 1)


def test_only_the_second_cards_latest_bill_is_unlinked() -> None:
    for anchor in _year_of_anchors():
        bills = build_demo_card_bills(anchor)
        unlinked = [b for b in bills if not b.linked]
        assert unlinked == [[b for b in bills if b.card == "card2"][-1]], anchor


def test_every_refund_original_is_an_earlier_spend_in_the_dataset() -> None:
    """A refund kept at an anchor always has its original kept too: the original is
    earlier, on the same account, in the same category."""
    for anchor in _year_of_anchors():
        spends, refunds, _income = build_demo_dataset(anchor)
        by_key = {(r.account, r.date, r.merchant): r for r in spends}
        for refund in refunds:
            if refund.refund_of is None:
                continue
            original = by_key[(refund.account, *refund.refund_of)]
            assert original.date <= refund.date
            assert original.category == refund.category


def test_every_card_and_bank_issuer_has_a_parser() -> None:
    """The accounts API refuses any edit of a card or bank whose issuer has no statement
    parser; the seeder writes the ORM directly, so nothing else would catch it."""
    for spec in ACCOUNTS:
        if spec.type in ("credit_card", "bank") and spec.issuer is not None:
            assert (spec.issuer, spec.type) in PARSERS, spec
        if spec.type == "credit_card":
            assert spec.issuer is not None and spec.parent is not None, spec


def test_instrument_dates_end_close_to_the_anchor() -> None:
    """The portfolio keeps up with the rolling spend window: no trade after the anchor,
    and the newest within the past month, so a monthly SIP never looks abandoned."""
    for anchor in _year_of_anchors():
        dates = [
            date.fromisoformat(t["date"])
            for spec in build_demo_instruments(anchor)
            for t in spec["txns"]
        ]
        assert max(dates) <= anchor
        assert (anchor - max(dates)).days < 31, anchor


def test_instrument_shift_keeps_each_instruments_trade_order() -> None:
    """One shift for all keeps the FIFO replay order (ADR-0015)."""
    for anchor in _year_of_anchors():
        for spec, template in zip(build_demo_instruments(anchor), INSTRUMENTS, strict=True):
            shifted = [t["date"] for t in spec["txns"]]
            assert shifted == sorted(shifted)
            assert len(shifted) == len(template["txns"])
