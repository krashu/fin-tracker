"""Tests for :class:`HdfcBank`, over synthetic fixtures for each known layout.

* ``estatement``: the 2024+ e-statement (Layout A, a ruled table packed one row per page).
* ``netbanking``: netbanking and the older e-statement (Layout B, a word table).
* ``netbanking_pre2020``: the pre-2020 statement (Layout B', a word table, ``0.00`` fill).

Every fixture is invented (name ``TEST USER``, all-zero account numbers, example handles)
and internally consistent: ``opening + Σ rows = closing`` and every printed balance chains.
Real-PDF checks run only when a local file and its password are present.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import asdict
from datetime import date
from typing import Any

import pytest

from app.parsers import HdfcBank, InvalidPasswordError, ParserError, RunningBalanceMismatchError
from app.parsers.base import StatementSummary, _decrypt, _extract_tables
from app.services.import_service import _map_type
from app.services.merchant import normalize_merchant
from app.services.reconciliation_service import is_card_bill_debit

LAYOUTS = ["estatement", "netbanking", "netbanking_pre2020"]

_Fixture = Callable[[str], Any]


def _serialize(rows: list[Any]) -> list[dict[str, Any]]:
    return [{**asdict(r), "date": r.date.isoformat()} for r in rows]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_interpret_tables_snapshot(layout: str, hdfc_bank_fixture: _Fixture) -> None:
    rows = HdfcBank.interpret_tables(hdfc_bank_fixture(f"tables_{layout}.json"))
    assert _serialize(rows) == hdfc_bank_fixture(f"tables_{layout}.expected.json")


@pytest.mark.parametrize("layout", LAYOUTS)
def test_interpret_summary_snapshot(layout: str, hdfc_bank_fixture: _Fixture) -> None:
    summary = HdfcBank.interpret_summary(hdfc_bank_fixture(f"summary_{layout}.txt"))
    expected = hdfc_bank_fixture(f"summary_{layout}.expected.json")
    assert summary == StatementSummary(
        opening_balance_paise=expected["opening_balance_paise"],
        closing_balance_paise=expected["closing_balance_paise"],
        period_start=date.fromisoformat(expected["period_start"]),
        period_end=date.fromisoformat(expected["period_end"]),
    )


@pytest.mark.parametrize("layout", LAYOUTS)
def test_fixture_is_self_consistent(layout: str, hdfc_bank_fixture: _Fixture) -> None:
    """The fixtures are only evidence if they obey the statement's own arithmetic."""
    expected = hdfc_bank_fixture(f"summary_{layout}.expected.json")
    rows = hdfc_bank_fixture(f"tables_{layout}.expected.json")
    total = expected["opening_balance_paise"] + sum(r["amount_paise"] for r in rows)
    assert total == expected["closing_balance_paise"]


def _alter_a(tables: list[Any]) -> None:
    tables[1][1][2] = "12,345.67\n2,100.00\n0.00"  # page 2, transaction 2: ATW withdrawal


def _alter_b(tables: list[Any]) -> None:
    tables[1][1][5] = "26.00"  # page 2, transaction 1: the cashback deposit


def _alter_b_prime(tables: list[Any]) -> None:
    tables[0][5][5] = "52,760.00"  # page 1, transaction 3: the salary deposit


@pytest.mark.parametrize(
    ("layout", "alter", "page", "row"),
    [
        ("estatement", _alter_a, 2, 2),
        ("netbanking", _alter_b, 2, 1),
        ("netbanking_pre2020", _alter_b_prime, 1, 3),
    ],
)
def test_running_balance_mismatch_raises(
    layout: str,
    alter: Callable[[list[Any]], None],
    page: int,
    row: int,
    hdfc_bank_fixture: _Fixture,
) -> None:
    """ADR-0010 rule 11: a row whose balance does not follow raises, naming page and row."""
    tables = copy.deepcopy(hdfc_bank_fixture(f"tables_{layout}.json"))
    alter(tables)
    with pytest.raises(RunningBalanceMismatchError) as info:
        HdfcBank.interpret_tables(tables)
    assert (info.value.page, info.value.row) == (page, row)
    assert str(info.value) == f"running balance does not match on page {page}, row {row}"


def test_failed_upi_reversal_is_a_positive_spend(hdfc_bank_fixture: _Fixture) -> None:
    """ADR-0010 rule 12: the returned debit nets the debit it reverses (ADR-0009)."""
    rows = HdfcBank.interpret_tables(hdfc_bank_fixture("tables_estatement.json"))
    debit, reversal = rows[2], rows[3]
    assert (debit.amount_paise, debit.txn_type) == (-49900, "withdrawal")
    assert (reversal.amount_paise, reversal.txn_type) == (49900, "refund")
    assert _map_type(debit) == "spend"
    assert _map_type(reversal) == "spend"
    assert debit.amount_paise + reversal.amount_paise == 0


def test_card_bill_debit_is_a_payment_candidate(hdfc_bank_fixture: _Fixture) -> None:
    """ADR-0010 rule 12: a debit naming a card-bill payment is flagged for F4a review."""
    rows = HdfcBank.interpret_tables(hdfc_bank_fixture("tables_estatement.json"))
    bill = rows[4]
    assert (bill.amount_paise, bill.txn_type) == (-1234567, "withdrawal")
    assert is_card_bill_debit(normalize_merchant(bill.merchant_raw))
    assert not any(
        is_card_bill_debit(normalize_merchant(r.merchant_raw)) for r in rows if r is not bill
    )


def test_cashback_deposit_imports_as_a_positive_spend(hdfc_bank_fixture: _Fixture) -> None:
    rows = HdfcBank.interpret_tables(hdfc_bank_fixture("tables_estatement.json"))
    assert (rows[6].txn_type, _map_type(rows[6])) == ("deposit", "spend")
    assert (rows[1].txn_type, _map_type(rows[1])) == ("deposit", "income")


def test_layout_a_packed_row_with_mismatched_cells_raises(hdfc_bank_fixture: _Fixture) -> None:
    tables = copy.deepcopy(hdfc_bank_fixture("tables_estatement.json"))
    tables[0][1][2] = "1,250.00\n0.00\n499.00"  # three withdrawals for four transactions
    with pytest.raises(ParserError, match=r"cannot read the transaction table on page 1"):
        HdfcBank.interpret_tables(tables)


def test_layout_a_ignores_the_summary_table(hdfc_bank_fixture: _Fixture) -> None:
    tables = hdfc_bank_fixture("tables_estatement.json")
    assert len(tables[1]) == 4  # header, the packed row, and the two summary-table rows
    assert len(HdfcBank.interpret_tables(tables)) == 7


def test_b_reader_skips_a_non_date_line_and_closes_the_row() -> None:
    header = [
        "Date",
        "Narration",
        "Chq./Ref.No.",
        "Value Dt",
        "Withdrawal Amt.",
        "Deposit Amt.",
        "Closing Balance",
    ]
    tables = [
        [
            header,
            ["01/04/26", "SENTINEL ONE", "", "", "100.00", "", "900.00"],
            ["MR TEST USER", "", "", "", "", "", ""],
            ["", "NOT PART OF ANY ROW", "", "", "", "", ""],
        ]
    ]
    rows = HdfcBank.interpret_tables(tables)
    assert [(r.merchant_raw, r.amount_paise) for r in rows] == [("SENTINEL ONE", -10000)]


def test_b_reader_rejects_a_second_amount_on_one_row() -> None:
    header = [
        "Date",
        "Narration",
        "Chq./Ref.No.",
        "Value Dt",
        "Withdrawal Amt.",
        "Deposit Amt.",
        "Closing Balance",
    ]
    tables = [
        [
            header,
            ["01/04/26", "SENTINEL ONE", "", "", "100.00", "", "900.00"],
            ["", "MORE", "", "", "", "", "800.00"],
        ]
    ]
    with pytest.raises(ParserError, match=r"cannot read row on page 1, row 1"):
        HdfcBank.interpret_tables(tables)


@pytest.mark.parametrize(("withdrawal", "deposit"), [("", ""), ("100.00", "5.00")])
def test_b_reader_needs_exactly_one_amount(withdrawal: str, deposit: str) -> None:
    header = [
        "Date",
        "Narration",
        "Chq./Ref.No.",
        "Value Dt",
        "Withdrawal Amt.",
        "Deposit Amt.",
        "Closing Balance",
    ]
    tables = [[header, ["01/04/26", "SENTINEL ONE", "", "", withdrawal, deposit, "900.00"]]]
    with pytest.raises(ParserError, match=r"(cannot read row|both a withdrawal) .*page 1, row 1"):
        HdfcBank.interpret_tables(tables)


def test_layout_a_rows_are_numbered_across_packed_rows_on_a_page() -> None:
    header = ["Txn Date", "Narration", "Withdrawals", "Deposits", "Closing Balance"]
    tables = [
        [
            header,
            ["01/04/2026", "SENTINEL ONE Value Dt 01/04/2026", "100.00", "0.00", "900.00"],
            ["02/04/2026", "SENTINEL TWO Value Dt 02/04/2026", "100.00", "0.00", "850.00"],
        ]
    ]
    with pytest.raises(RunningBalanceMismatchError) as info:
        HdfcBank.interpret_tables(tables)
    assert (info.value.page, info.value.row) == (1, 2)


def test_no_table_header_raises() -> None:
    """ADR-0010 rule 5: a reader that never finds its header refuses the file."""
    tables = [[["01/04/26", "SENTINEL ONE", "", "", "100.00", "", "900.00"]]]
    with pytest.raises(ParserError, match=r"cannot find the transaction table"):
        HdfcBank.interpret_tables(tables)


@pytest.mark.parametrize(
    "header",
    [
        ["Txn Date", "Narration", "Withdrawals", "Deposits", "Closing Balance"],
        [
            "Date",
            "Narration",
            "Chq./Ref.No.",
            "Value Dt",
            "Withdrawal Amt.",
            "Deposit Amt.",
            "Closing Balance",
        ],
    ],
)
def test_header_with_no_rows_is_an_empty_statement(header: list[str]) -> None:
    """ADR-0010 rule 5: a header with no rows under it is a valid 0-row batch."""
    assert HdfcBank.interpret_tables([[header]]) == []


def test_interpret_summary_bank_sign_overdrawn_is_negative() -> None:
    """A bank prints money held bare, so a bare figure is positive and ``-`` or ``Dr`` is an
    overdraft (ADR-0010 rule 7). The marker sits after the amount, so each line is scanned
    for every amount token rather than split on whitespace."""
    head = "Opening Balance Debit Amount Credit Amount Closing Balance"
    overdrawn = HdfcBank.interpret_summary([head, "500.00 Dr 10.00 20.00 -500.00"])
    assert (overdrawn.opening_balance_paise, overdrawn.closing_balance_paise) == (-50000, -50000)
    held = HdfcBank.interpret_summary([head, "500.00 10.00 20.00 510.00"])
    assert (held.opening_balance_paise, held.closing_balance_paise) == (50000, 51000)
    credit_marker = HdfcBank.interpret_summary([head, "500.00 Cr 10.00 20.00 510.00 Cr"])
    assert credit_marker.opening_balance_paise == 50000


def test_interpret_summary_no_labels_returns_all_none() -> None:
    assert HdfcBank.interpret_summary(["nothing", "to see here"]) == StatementSummary()


# ---------------------------------------------------------------------------
# parse(): the whole path over a synthetic PDF.
# ---------------------------------------------------------------------------

_Pages = list[list[tuple[float, float, str]]]


def _netbanking_page(opening: str) -> _Pages:
    columns = [
        (30, "Date"),
        (80, "Narration"),
        (230, "Chq./Ref.No."),
        (300, "Value Dt"),
        (350, "Withdrawal Amt."),
        (420, "Deposit Amt."),
        (490, "Closing Balance"),
    ]
    page: list[tuple[float, float, str]] = [
        (30, 760, "Statement of account From : 01/04/2026 To : 30/04/2026")
    ]
    page += [(x, 700, label) for x, label in columns]
    for y, date_text, narration, withdrawal, deposit, balance in (
        (686, "01/04/26", "UPI-SENTINEL GROCER", "1,250.00", "", "198,750.00"),
        (672, "02/04/26", "NEFT CR-SENTINEL", "", "75,000.00", "273,750.00"),
        (658, "03/04/26", "UPI-SENTINEL FOOD", "500.00", "", "273,250.00"),
    ):
        page += [(30, y, date_text), (80, y, narration), (300, y, date_text)]
        if withdrawal:
            page.append((345, y, withdrawal))
        if deposit:
            page.append((425, y, deposit))
        page.append((480, y, balance))
    page += [
        (30, 600, "STATEMENT SUMMARY :-"),
        (30, 588, "Opening Balance Dr Count Cr Count Debits Credits Closing Bal"),
        (30, 576, f"{opening} 2 1 1,750.00 75,000.00 273,250.00"),
    ]
    return [page]


def test_parse_netbanking_pdf_reads_rows_and_summary(
    positioned_pdf_bytes: Callable[[_Pages], bytes],
) -> None:
    parsed = HdfcBank.parse(positioned_pdf_bytes(_netbanking_page("200,000.00")), password=None)
    assert [(r.date, r.amount_paise, r.txn_type) for r in parsed.rows] == [
        (date(2026, 4, 1), -125000, "withdrawal"),
        (date(2026, 4, 2), 7500000, "deposit"),
        (date(2026, 4, 3), -50000, "withdrawal"),
    ]
    assert parsed.summary == StatementSummary(
        opening_balance_paise=20000000,
        closing_balance_paise=27325000,
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
    )
    assert parsed.summary.opening_balance_paise is not None
    assert parsed.summary.opening_balance_paise + sum(r.amount_paise for r in parsed.rows) == (
        parsed.summary.closing_balance_paise
    )


def _two_page_netbanking(stop: str) -> _Pages:
    """A Layout B statement that repeats its column header on page 2 under a stop line.

    Models a Layout B that repeats its column header on later pages, which the real-PDF
    check has not confirmed (S29 R1).
    """
    columns = [
        (30, "Date"),
        (80, "Narration"),
        (230, "Chq./Ref.No."),
        (300, "Value Dt"),
        (350, "Withdrawal Amt."),
        (420, "Deposit Amt."),
        (490, "Closing Balance"),
    ]

    def row(
        y: float, date_text: str, narration: str, withdrawal: str, deposit: str, balance: str
    ) -> list[tuple[float, float, str]]:
        cells = [(30, y, date_text), (80, y, narration), (300, y, date_text)]
        if withdrawal:
            cells.append((345, y, withdrawal))
        if deposit:
            cells.append((425, y, deposit))
        return [*cells, (480, y, balance)]

    page_one: list[tuple[float, float, str]] = [
        (30, 760, "Statement of account From : 01/04/2026 To : 30/04/2026"),
        *[(x, 700, label) for x, label in columns],
        *row(686, "01/04/26", "UPI-SENTINEL GROCER", "1,250.00", "", "198,750.00"),
        *row(672, "02/04/26", "NEFT CR-SENTINEL", "", "75,000.00", "273,750.00"),
        (30, 60, "HDFC BANK LIMITED"),
    ]
    page_two: list[tuple[float, float, str]] = [
        (30, 770, stop),
        (30, 755, "MR TEST USER"),
        *[(x, 700, label) for x, label in columns],
        *row(686, "03/04/26", "UPI-SENTINEL FOOD", "500.00", "", "273,250.00"),
        (30, 600, "STATEMENT SUMMARY :-"),
        (30, 588, "Opening Balance Dr Count Cr Count Debits Credits Closing Bal"),
        (30, 576, "200,000.00 2 1 1,750.00 75,000.00 273,250.00"),
    ]
    return [page_one, page_two]


@pytest.mark.parametrize("stop", ["Page No .: 2", "HDFC BANK LIMITED"])
def test_parse_reads_a_page_with_a_stop_line_above_its_header(
    positioned_pdf_bytes: Callable[[_Pages], bytes], stop: str
) -> None:
    parsed = HdfcBank.parse(positioned_pdf_bytes(_two_page_netbanking(stop)), password=None)
    assert [(r.date, r.amount_paise, r.txn_type) for r in parsed.rows] == [
        (date(2026, 4, 1), -125000, "withdrawal"),
        (date(2026, 4, 2), 7500000, "deposit"),
        (date(2026, 4, 3), -50000, "withdrawal"),
    ]
    assert parsed.summary.opening_balance_paise is not None
    assert parsed.summary.opening_balance_paise + sum(r.amount_paise for r in parsed.rows) == (
        parsed.summary.closing_balance_paise
    )


def test_parse_checks_the_first_row_against_the_opening(
    positioned_pdf_bytes: Callable[[_Pages], bytes],
) -> None:
    """``interpret_tables`` alone never sees the opening balance, so ``parse`` does the
    row-1 check (ADR-0010 rule 11)."""
    pdf = positioned_pdf_bytes(_netbanking_page("201,000.00"))
    with pytest.raises(RunningBalanceMismatchError) as info:
        HdfcBank.parse(pdf, password=None)
    assert (info.value.page, info.value.row) == (1, 1)


def test_parse_corrupt_bytes_raises() -> None:
    with pytest.raises(ParserError):
        HdfcBank.parse(b"this is definitely not a pdf", password=None)


def test_parse_wrong_password_raises(encrypted_pdf_bytes: bytes) -> None:
    with pytest.raises(InvalidPasswordError):
        HdfcBank.parse(encrypted_pdf_bytes, password="definitely-not-the-right-password-xyz")


def test_parse_without_a_table_header_raises(
    text_pdf_bytes: Callable[[list[str]], bytes],
) -> None:
    """A summary block with no transaction table is refused, not imported as 0 rows."""
    pdf = text_pdf_bytes(
        [
            "Statement of account From : 01/04/2026 To : 30/04/2026",
            "Opening Balance Debit Amount Credit Amount Closing Balance",
            "200,000.00 0.00 0.00 200,000.00",
        ]
    )
    with pytest.raises(ParserError, match=r"cannot find the transaction table"):
        HdfcBank.parse(pdf, password=None)


def _shape(pdf: bytes, password: str) -> str:
    """A PII-free account of the extracted tables, for a failure message: counts and the
    header labels only, never a cell's content."""
    pages = _extract_tables(_decrypt(pdf, password))
    parts = []
    for i, rows in enumerate(pages, start=1):
        widths = sorted({len(r) for r in rows})
        parts.append(f"page {i}: {len(rows)} rows, cell counts {widths}")
    return "; ".join(parts)


@pytest.mark.real_pdf
def test_parse_real_pdf_smoke(hdfc_bank_real_pdf: bytes, hdfc_bank_real_password: str) -> None:
    """End to end on the user's own statement. Failure text carries integers and counts only."""
    try:
        parsed = HdfcBank.parse(hdfc_bank_real_pdf, password=hdfc_bank_real_password)
    except RunningBalanceMismatchError as e:
        pytest.fail(f"running balance mismatch on page {e.page}, row {e.row}")
    except ParserError as e:
        pytest.fail(
            f"parser error ({type(e).__name__}): {e}; "
            f"{_shape(hdfc_bank_real_pdf, hdfc_bank_real_password)}"
        )
    summary = parsed.summary
    assert parsed.rows, "no rows read"
    assert summary.opening_balance_paise is not None, "no opening balance read"
    assert summary.closing_balance_paise is not None, "no closing balance read"
    assert summary.period_start is not None and summary.period_end is not None, "no period read"
    total = summary.opening_balance_paise + sum(r.amount_paise for r in parsed.rows)
    assert total == summary.closing_balance_paise, (
        f"opening + rows differs from closing by {total - summary.closing_balance_paise} paise"
    )
