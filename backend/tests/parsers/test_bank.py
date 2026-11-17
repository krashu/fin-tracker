"""The shared bank-row contract: balance cells, typing, the running-balance check (ADR-0010)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import pytest

from app.parsers.bank import (
    BankRow,
    OpenRow,
    classify_bank_row,
    parse_bank_amount,
    parse_bank_balance,
    to_transactions,
)
from app.parsers.base import ParserError, RunningBalanceMismatchError


def _row(
    *,
    withdrawal: int = 0,
    deposit: int = 0,
    balance: int,
    page: int = 1,
    row: int = 1,
    day: int = 1,
    narration: str = "SENTINEL",
) -> BankRow:
    return BankRow(
        date=date(2026, 4, day),
        narration=narration,
        withdrawal_paise=withdrawal,
        deposit_paise=deposit,
        balance_paise=balance,
        page=page,
        row=row,
    )


def test_first_row_checked_against_opening() -> None:
    rows = [_row(withdrawal=500, balance=9_500)]
    assert to_transactions(rows, opening_paise=10_000)[0].amount_paise == -500
    with pytest.raises(RunningBalanceMismatchError) as info:
        to_transactions(rows, opening_paise=11_000)
    assert (info.value.page, info.value.row) == (1, 1)


def test_without_opening_the_check_starts_at_row_two() -> None:
    # Row 1's balance cannot be checked (no opening); row 2 chains from row 1's balance.
    good = [_row(withdrawal=500, balance=9_500), _row(deposit=200, balance=9_700, row=2)]
    assert [t.amount_paise for t in to_transactions(good, opening_paise=None)] == [-500, 200]
    bad = [_row(withdrawal=500, balance=9_500), _row(deposit=200, balance=9_800, row=2, page=2)]
    with pytest.raises(RunningBalanceMismatchError) as info:
        to_transactions(bad, opening_paise=None)
    assert (info.value.page, info.value.row) == (2, 2)


def test_row_with_both_sides_raises() -> None:
    with pytest.raises(ParserError, match=r"both a withdrawal and a deposit on page 3, row 4"):
        to_transactions([_row(withdrawal=1, deposit=1, balance=0, page=3, row=4)], opening_paise=0)


def test_rows_sorted_by_date_keeping_page_and_row_order() -> None:
    # Printed order is the chain order; the output order is (date, page, row).
    rows = [
        _row(withdrawal=100, balance=900, day=5, page=1, row=1, narration="LATE"),
        _row(withdrawal=100, balance=800, day=3, page=1, row=2, narration="P1"),
        _row(withdrawal=100, balance=700, day=3, page=2, row=1, narration="P2"),
        _row(withdrawal=100, balance=600, day=3, page=2, row=2, narration="P2R2"),
    ]
    out = to_transactions(rows, opening_paise=1_000)
    assert [t.merchant_raw for t in out] == ["P1", "P2", "P2R2", "LATE"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2,00,000.00", 20_000_000),
        ("200,000.00", 20_000_000),
        ("500.00 Dr", -50_000),
        ("500.00 DR", -50_000),
        ("-500.00", -50_000),
        ("500.00 Cr", 50_000),
        ("0.00", 0),
        ("", None),
        ("Closing Balance", None),
    ],
)
def test_parse_bank_balance_sign(text: str, expected: int | None) -> None:
    assert parse_bank_balance(text) == expected


@pytest.mark.parametrize(("cell", "expected"), [("", 0), ("0.00", 0), ("1,250.00", 125_000)])
def test_parse_bank_amount(cell: str, expected: int) -> None:
    assert parse_bank_amount(cell) == expected


@pytest.mark.parametrize("cell", ["abc", "-5.00", "5.00 Cr"])
def test_parse_bank_amount_rejects_unreadable(cell: str) -> None:
    with pytest.raises(ParserError) as info:
        parse_bank_amount(cell)
    assert cell not in str(info.value)


@pytest.mark.parametrize(
    ("narration", "is_deposit", "expected"),
    [
        ("REV-UPI-000000000003-SENTINEL FOOD", True, "refund"),
        ("UPI/000000000003/SENTINEL FOOD/REV", True, "refund"),
        ("REFUND SENTINEL SHOP", True, "refund"),
        ("CHARGEBACK SENTINEL SHOP", True, "refund"),
        ("NEFT CR-SENTINEL EMPLOYER", True, "deposit"),
        ("REVENUE SHARE SENTINEL", True, "deposit"),
        ("REV-UPI-000000000003", False, "withdrawal"),
        ("UPI-SENTINEL GROCER", False, "withdrawal"),
    ],
)
def test_classify_bank_row(narration: str, is_deposit: bool, expected: str) -> None:
    assert classify_bank_row(narration, is_deposit=is_deposit) == expected


# ---------------------------------------------------------------------------
# OpenRow: one printed row built from its date line and the lines that continue it.
# ---------------------------------------------------------------------------


def _outcome(fn: Callable[[], BankRow]) -> BankRow | str:
    """The closed row, or the error text, so a wrongly raised error fails as an assertion."""
    try:
        return fn()
    except ParserError as e:
        return f"ParserError: {e}"


def _open(**amounts: str) -> OpenRow:
    row = OpenRow(date(2026, 4, 1), 2, 3)
    row.absorb(["UPI-SENTINEL"], **amounts)
    return row


def test_open_row_joins_wrapped_narration_and_continuation_amounts() -> None:
    row = _open(balance="900.00")
    row.absorb(["  GROCER   MUMBAI "], withdrawal="100.00")
    assert _outcome(row.close) == BankRow(
        date=date(2026, 4, 1),
        narration="UPI-SENTINEL GROCER MUMBAI",
        withdrawal_paise=10000,
        deposit_paise=0,
        balance_paise=90000,
        page=2,
        row=3,
    )


def test_open_row_rejects_a_second_value_for_one_column() -> None:
    row = _open(withdrawal="100.00", balance="900.00")
    with pytest.raises(ParserError, match=r"cannot read row on page 2, row 3"):
        row.absorb([], withdrawal="100.00")


def test_open_row_blank_then_value_fills_the_slot() -> None:
    def build() -> BankRow:
        row = _open(balance="900.00")
        row.absorb([], withdrawal="   ")
        row.absorb([], withdrawal="100.00")
        return row.close()

    closed = _outcome(build)
    assert isinstance(closed, BankRow)
    assert closed.withdrawal_paise == 10000


@pytest.mark.parametrize(
    "amounts",
    [
        pytest.param({"balance": "900.00"}, id="neither"),
        pytest.param({"withdrawal": "100.00", "deposit": "50.00", "balance": "900.00"}, id="both"),
    ],
)
def test_open_row_needs_exactly_one_amount(amounts: dict[str, str]) -> None:
    assert _outcome(_open(**amounts).close) == "ParserError: cannot read row on page 2, row 3"


@pytest.mark.parametrize("balance", ["", "N/A"])
def test_open_row_needs_a_readable_balance(balance: str) -> None:
    row = _open(withdrawal="100.00", balance=balance)
    assert _outcome(row.close) == "ParserError: cannot read row on page 2, row 3"


def test_open_row_without_a_date_is_unreadable() -> None:
    row = OpenRow(None, 2, 3)
    row.absorb(["SENTINEL"], withdrawal="100.00", balance="900.00")
    assert _outcome(row.close) == "ParserError: cannot read row on page 2, row 3"


def test_open_row_close_uses_the_given_join() -> None:
    row = _open(withdrawal="100.00", balance="900.00")
    row.absorb(["GROCER"])
    closed = _outcome(lambda: row.close(join=lambda parts: "/".join(parts)))
    assert isinstance(closed, BankRow)
    assert closed.narration == "UPI-SENTINEL/GROCER"
