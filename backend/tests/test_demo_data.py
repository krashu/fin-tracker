"""Unit tests for the pure demo-dataset generator (app.core.demo_data).

``build_demo_dataset`` has no DB/ORM dependency, so these test it directly
rather than through the seeder — in particular the current-month truncation
that keeps a synced demo account from ever showing a future-dated transaction,
which is easy to leave unexercised when every other test picks a mid-month
anchor.
"""

from __future__ import annotations

from datetime import date

from app.core.demo_data import DEMO_WINDOW_MONTHS, build_demo_dataset


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
