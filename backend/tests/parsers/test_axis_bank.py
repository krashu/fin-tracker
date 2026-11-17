"""Tests for :class:`AxisBank`, over synthetic fixtures for each known layout.

* ``current``: the current statement (header ends ``Init.(Br)``, uncomma'd opening/closing,
  ``TRANSACTION TOTAL`` row, a legend line after the table).
* ``legacy``: the older statement (header ends ``Init.``, comma-grouped opening/closing,
  a ``<token> TOTAL`` row, an ``End of Statement`` footer).

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

from app.parsers import AxisBank, InvalidPasswordError, ParserError, RunningBalanceMismatchError
from app.parsers.axis_bank import _HEADERS, _STOP_RE
from app.parsers.base import StatementSummary, _decrypt, _extract_word_table
from app.services.import_service import _map_type
from app.services.merchant import normalize_merchant
from app.services.reconciliation_service import is_card_bill_debit

LAYOUTS = ["current", "legacy"]

_Fixture = Callable[[str], Any]

_HEADER = ["Tran Date", "Chq No", "Particulars", "Debit", "Credit", "Balance", "Init.(Br)"]
_TITLE = (
    "Statement of Axis Account No :000000000000000 for the period "
    "(From : 01-04-2026 To : 30-04-2026)"
)


def _serialize(rows: list[Any]) -> list[dict[str, Any]]:
    return [{**asdict(r), "date": r.date.isoformat()} for r in rows]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_interpret_tables_snapshot(layout: str, axis_bank_fixture: _Fixture) -> None:
    rows = AxisBank.interpret_tables(axis_bank_fixture(f"tables_{layout}.json"))
    assert _serialize(rows) == axis_bank_fixture(f"tables_{layout}.expected.json")


@pytest.mark.parametrize("layout", LAYOUTS)
def test_interpret_summary_snapshot(layout: str, axis_bank_fixture: _Fixture) -> None:
    summary = AxisBank.interpret_summary(axis_bank_fixture(f"summary_{layout}.txt"))
    expected = axis_bank_fixture(f"summary_{layout}.expected.json")
    assert summary == StatementSummary(
        opening_balance_paise=expected["opening_balance_paise"],
        closing_balance_paise=expected["closing_balance_paise"],
        period_start=date.fromisoformat(expected["period_start"]),
        period_end=date.fromisoformat(expected["period_end"]),
    )


@pytest.mark.parametrize("layout", LAYOUTS)
def test_fixture_is_self_consistent(layout: str, axis_bank_fixture: _Fixture) -> None:
    """The fixtures are only evidence if they obey the statement's own arithmetic."""
    expected = axis_bank_fixture(f"summary_{layout}.expected.json")
    rows = axis_bank_fixture(f"tables_{layout}.expected.json")
    total = expected["opening_balance_paise"] + sum(r["amount_paise"] for r in rows)
    assert total == expected["closing_balance_paise"]


def _alter_opening(tables: list[Any]) -> None:
    tables[0][1][5] = "25001.00"  # the in-table opening balance, so row 1 no longer follows


def _alter_page2(tables: list[Any]) -> None:
    tables[1][1][5] = "85553.84"  # page 2, transaction 1: the pharmacy debit


def _alter_legacy(tables: list[Any]) -> None:
    tables[0][4][5] = "143000.01"  # page 1, transaction 3: the tenant cheque deposit


@pytest.mark.parametrize(
    ("layout", "alter", "page", "row"),
    [
        pytest.param("current", _alter_opening, 1, 1, id="current-opening"),
        pytest.param("current", _alter_page2, 2, 1, id="current-page2"),
        pytest.param("legacy", _alter_legacy, 1, 3, id="legacy"),
    ],
)
def test_running_balance_mismatch_raises(
    layout: str,
    alter: Callable[[list[Any]], None],
    page: int,
    row: int,
    axis_bank_fixture: _Fixture,
) -> None:
    """ADR-0010 rule 11: a row whose balance does not follow raises, naming page and row.

    ``current-opening`` also locks the in-table ``OPENING BALANCE`` row seeding the row-1
    check inside ``interpret_tables``."""
    tables = copy.deepcopy(axis_bank_fixture(f"tables_{layout}.json"))
    alter(tables)
    with pytest.raises(RunningBalanceMismatchError) as info:
        AxisBank.interpret_tables(tables)
    assert (info.value.page, info.value.row) == (page, row)
    assert str(info.value) == f"running balance does not match on page {page}, row {row}"


def test_failed_upi_reversal_is_a_positive_spend(axis_bank_fixture: _Fixture) -> None:
    """ADR-0010 rule 12: the returned debit nets the debit it reverses (ADR-0009)."""
    rows = AxisBank.interpret_tables(axis_bank_fixture("tables_current.json"))
    debit, reversal = rows[2], rows[3]
    assert (debit.amount_paise, debit.txn_type) == (-49900, "withdrawal")
    assert (reversal.amount_paise, reversal.txn_type) == (49900, "refund")
    assert _map_type(debit) == "spend"
    assert _map_type(reversal) == "spend"
    assert debit.amount_paise + reversal.amount_paise == 0


def test_card_bill_debit_is_a_payment_candidate(axis_bank_fixture: _Fixture) -> None:
    """ADR-0010 rule 12: a debit naming a card-bill payment is flagged for F4a review."""
    rows = AxisBank.interpret_tables(axis_bank_fixture("tables_current.json"))
    bill = rows[4]
    assert (bill.amount_paise, bill.txn_type) == (-1234567, "withdrawal")
    assert is_card_bill_debit(normalize_merchant(bill.merchant_raw))
    assert not any(
        is_card_bill_debit(normalize_merchant(r.merchant_raw)) for r in rows if r is not bill
    )


def test_rows_after_the_transaction_total_are_ignored() -> None:
    tables = [
        [
            _HEADER,
            ["", "", "OPENING BALANCE", "", "", "1000.00", ""],
            ["01-04-2026", "", "SENTINEL ONE", "100.00", "", "900.00", "000"],
            ["", "", "TRANSACTION TOTAL", "100.00", "", "", ""],
            ["15-04-2026", "", "SENTINEL FD", "", "", "5000.00", ""],
        ]
    ]
    rows = AxisBank.interpret_tables(tables)
    assert [(r.merchant_raw, r.amount_paise) for r in rows] == [("SENTINEL ONE", -10000)]


def test_a_later_opening_row_is_not_narration() -> None:
    tables = [
        [
            _HEADER,
            ["", "", "OPENING BALANCE", "", "", "1000.00", ""],
            ["01-04-2026", "", "SENTINEL ONE", "100.00", "", "900.00", "000"],
            ["", "", "OPENING BALANCE", "", "", "900.00", ""],
            ["02-04-2026", "", "SENTINEL TWO", "100.00", "", "800.00", "000"],
        ]
    ]
    rows = AxisBank.interpret_tables(tables)
    assert [r.merchant_raw for r in rows] == ["SENTINEL ONE", "SENTINEL TWO"]


@pytest.mark.parametrize(("debit", "credit"), [("", ""), ("100.00", "5.00")])
def test_row_needs_exactly_one_amount(debit: str, credit: str) -> None:
    tables = [[_HEADER, ["01-04-2026", "", "SENTINEL ONE", debit, credit, "900.00", "000"]]]
    with pytest.raises(ParserError, match=r"(cannot read row|both a withdrawal) .*page 1, row 1"):
        AxisBank.interpret_tables(tables)


def test_row_rejects_a_second_amount_in_one_column() -> None:
    tables = [
        [
            _HEADER,
            ["01-04-2026", "", "SENTINEL ONE", "100.00", "", "900.00", "000"],
            ["", "", "MORE", "50.00", "", "", ""],
        ]
    ]
    with pytest.raises(ParserError, match=r"cannot read row on page 1, row 1"):
        AxisBank.interpret_tables(tables)


def test_unreadable_header_raises() -> None:
    with pytest.raises(ParserError, match=r"cannot read the transaction table header on page 1"):
        AxisBank.interpret_tables([[["Tran Date", "Particulars", "Balance"]]])


def test_tables_without_a_header_are_refused() -> None:
    """ADR-0010 rule 5 (A4): a reader that never found its table raises."""
    with pytest.raises(ParserError, match=r"^cannot find the transaction table$"):
        AxisBank.interpret_tables([[["Statement of Axis Account No :000000000000000"]]])


def test_interpret_summary_bank_sign_overdrawn_is_negative() -> None:
    """A bank prints money held bare, so a bare figure is positive and ``-`` or ``Dr`` is an
    overdraft (ADR-0010 rule 7)."""
    overdrawn = AxisBank.interpret_summary(["OPENING BALANCE -500.00", "CLOSING BALANCE 500.00 Dr"])
    assert (overdrawn.opening_balance_paise, overdrawn.closing_balance_paise) == (-50000, -50000)
    held = AxisBank.interpret_summary(["OPENING BALANCE 500.00 Cr"])
    assert held.opening_balance_paise == 50000


def test_interpret_summary_no_labels_returns_all_none() -> None:
    lines = [
        "Statement of Axis Account No :000000000000000",
        "TRANSACTION TOTAL 16445.17 76999.00",
        "Balance 500.00",
    ]
    assert AxisBank.interpret_summary(lines) == StatementSummary()


# ---------------------------------------------------------------------------
# parse(): the whole path over a synthetic PDF.
# ---------------------------------------------------------------------------

_Pages = list[list[tuple[float, float, str]]]

_ROWS = [
    (672, "01-04-2026", "UPI/SENTINEL GROCER", "1250.00", "", "23750.00"),
    (658, "02-04-2026", "NEFT/SENTINEL EMPLOYER", "", "75000.00", "98750.00"),
    (644, "03-04-2026", "UPI/SENTINEL FOOD", "499.00", "", "98251.00"),
]


def _axis_page(
    opening: str,
    *,
    init_label: str = "Init.(Br)",
    opening_above_header: bool = False,
    rows: list[tuple[float, str, str, str, str, str]] = _ROWS,
    total: tuple[str, str] = ("1749.00", "75000.00"),
    closing: str = "98251.00",
) -> _Pages:
    columns = [
        (30, "Tran Date"),
        (85, "Chq No"),
        (130, "Particulars"),
        (330, "Debit"),
        (400, "Credit"),
        (470, "Balance"),
        (540, init_label),
    ]
    page: list[tuple[float, float, str]] = [(30, 760, _TITLE)]
    page += [(x, 700, label) for x, label in columns]
    opening_y = 720 if opening_above_header else 686
    page += [(130, opening_y, "OPENING BALANCE"), (465, opening_y, opening)]
    for y, date_text, narration, debit, credit, balance in rows:
        page += [(30, y, date_text), (130, y, narration)]
        page.append((325, y, debit) if debit else (395, y, credit))
        page += [(465, y, balance), (545, y, "000")]
    page += [
        (130, 630, "TRANSACTION TOTAL"),
        (325, 630, total[0]),
        (395, 630, total[1]),
        (130, 616, "CLOSING BALANCE"),
        (465, 616, closing),
        (30, 590, "Unless the constituent notifies the bank of any discrepancy"),
    ]
    return [page]


@pytest.mark.parametrize("init_label", ["Init.(Br)", "Init."])
def test_parse_reads_rows_and_summary(
    init_label: str, positioned_pdf_bytes: Callable[[_Pages], bytes]
) -> None:
    pdf = positioned_pdf_bytes(_axis_page("25000.00", init_label=init_label))
    parsed = AxisBank.parse(pdf, password=None)
    assert [(r.date, r.amount_paise, r.txn_type) for r in parsed.rows] == [
        (date(2026, 4, 1), -125000, "withdrawal"),
        (date(2026, 4, 2), 7500000, "deposit"),
        (date(2026, 4, 3), -49900, "withdrawal"),
    ]
    assert parsed.summary == StatementSummary(
        opening_balance_paise=2500000,
        closing_balance_paise=9825100,
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
    )
    assert parsed.summary.opening_balance_paise is not None
    assert parsed.summary.opening_balance_paise + sum(r.amount_paise for r in parsed.rows) == (
        parsed.summary.closing_balance_paise
    )


@pytest.mark.parametrize("above_header", [False, True], ids=["in_table", "above_header"])
def test_parse_checks_the_first_row_against_the_opening(
    above_header: bool, positioned_pdf_bytes: Callable[[_Pages], bytes]
) -> None:
    """The opening balance seeds the row-1 check whether the PDF prints it inside the table
    or above the header, where only the summary sees it (ADR-0010 rule 11)."""
    pdf = positioned_pdf_bytes(_axis_page("25001.00", opening_above_header=above_header))
    with pytest.raises(RunningBalanceMismatchError) as info:
        AxisBank.parse(pdf, password=None)
    assert (info.value.page, info.value.row) == (1, 1)


def test_zero_rows_with_summary_is_a_valid_statement(
    positioned_pdf_bytes: Callable[[_Pages], bytes],
) -> None:
    """A month with no transactions still prints a header and its balances: no rows and the
    summary, never a ParserError (ADR-0010 rule 5)."""
    pdf = positioned_pdf_bytes(
        _axis_page("1000.00", rows=[], total=("0.00", "0.00"), closing="1000.00")
    )
    parsed = AxisBank.parse(pdf, password=None)
    assert parsed.rows == []
    assert parsed.summary == StatementSummary(
        opening_balance_paise=100000,
        closing_balance_paise=100000,
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
    )


def test_statement_without_a_table_header_is_refused(
    text_pdf_bytes: Callable[[list[str]], bytes],
) -> None:
    """ADR-0010 rule 5 (A4): a header that was never found is not a 0-row statement."""
    pdf = text_pdf_bytes([_TITLE, "OPENING BALANCE 1000.00", "CLOSING BALANCE 1000.00"])
    with pytest.raises(ParserError, match=r"^cannot find the transaction table$"):
        AxisBank.parse(pdf, password=None)


def test_parse_corrupt_bytes_raises() -> None:
    with pytest.raises(ParserError):
        AxisBank.parse(b"this is definitely not a pdf", password=None)


def test_parse_wrong_password_raises(encrypted_pdf_bytes: bytes) -> None:
    with pytest.raises(InvalidPasswordError):
        AxisBank.parse(encrypted_pdf_bytes, password="definitely-not-the-right-password-xyz")


def _shape(pdf: bytes, password: str) -> str:
    """A PII-free account of the extracted word table, for a failure message: counts and the
    matched header's last label only, never a cell's content."""
    pages = _extract_word_table(_decrypt(pdf, password), headers=_HEADERS, stop_re=_STOP_RE)
    parts = []
    for i, rows in enumerate(pages, start=1):
        widths = sorted({len(r) for r in rows})
        label = rows[0][-1] if rows and len(rows[0]) == len(_HEADER) else "no header"
        parts.append(f"page {i}: {len(rows)} rows, cell counts {widths}, header ends {label!r}")
    return "; ".join(parts)


@pytest.mark.real_pdf
def test_parse_real_pdf_smoke(axis_bank_real_pdf: bytes, axis_bank_real_password: str) -> None:
    """End to end on the user's own statement. Failure text carries integers and counts only;
    ``header ends`` names the layout (``Init.(Br)`` current, ``Init.`` legacy)."""
    try:
        parsed = AxisBank.parse(axis_bank_real_pdf, password=axis_bank_real_password)
    except RunningBalanceMismatchError as e:
        pytest.fail(f"running balance mismatch on page {e.page}, row {e.row}")
    except ParserError as e:
        pytest.fail(
            f"parser error ({type(e).__name__}): {e}; "
            f"{_shape(axis_bank_real_pdf, axis_bank_real_password)}"
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
