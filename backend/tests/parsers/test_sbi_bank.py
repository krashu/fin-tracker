"""Tests for :class:`SbiBank`, over synthetic fixtures for each known layout.

* ``netbanking``: the onlinesbi "Account Statement" (year wraps onto the next line).
* ``estatement``: the e-account statement (Credit printed before Debit).
* ``yono``: the YONO "STATEMENT OF ACCOUNT" (only Balance labelled).

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

import app.parsers.sbi_bank as sbi_bank
from app.parsers import InvalidPasswordError, ParserError, RunningBalanceMismatchError, SbiBank
from app.parsers.base import StatementSummary, _decrypt, _extract_tables
from app.services import import_service
from app.services.import_service import _map_type
from app.services.merchant import normalize_merchant
from app.services.reconciliation_service import is_card_bill_debit

LAYOUTS = ["netbanking", "estatement", "yono"]

_Fixture = Callable[[str], Any]

_NB_HEADER = ["Txn Date", "Value", "Description", "Ref No./Cheque", "Debit", "Credit", "Balance"]
_ES_HEADER = [
    "Date",
    "Transaction Reference",
    "",
    "",
    "Ref.No./Chq.No.",
    "Credit",
    "Debit",
    "Balance",
]


def _serialize(rows: list[Any]) -> list[dict[str, Any]]:
    return [{**asdict(r), "date": r.date.isoformat()} for r in rows]


def _summary(expected: dict[str, Any]) -> StatementSummary:
    return StatementSummary(
        opening_balance_paise=expected["opening_balance_paise"],
        closing_balance_paise=expected["closing_balance_paise"],
        period_start=date.fromisoformat(expected["period_start"]),
        period_end=date.fromisoformat(expected["period_end"]),
    )


def _flat(rows: list[Any]) -> list[tuple[int, str]]:
    return [(r.amount_paise, r.txn_type) for r in rows]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_interpret_tables_snapshot(layout: str, sbi_bank_fixture: _Fixture) -> None:
    rows = SbiBank.interpret_tables(sbi_bank_fixture(f"tables_{layout}.json"))
    assert _serialize(rows) == sbi_bank_fixture(f"tables_{layout}.expected.json")


@pytest.mark.parametrize("layout", LAYOUTS)
def test_interpret_summary_snapshot(layout: str, sbi_bank_fixture: _Fixture) -> None:
    summary = SbiBank.interpret_summary(sbi_bank_fixture(f"summary_{layout}.txt"))
    assert summary == _summary(sbi_bank_fixture(f"summary_{layout}.expected.json"))


@pytest.mark.parametrize("layout", LAYOUTS)
def test_fixture_is_self_consistent(layout: str, sbi_bank_fixture: _Fixture) -> None:
    """The fixtures are only evidence if they obey the statement's own arithmetic."""
    expected = sbi_bank_fixture(f"summary_{layout}.expected.json")
    rows = sbi_bank_fixture(f"tables_{layout}.expected.json")
    total = expected["opening_balance_paise"] + sum(r["amount_paise"] for r in rows)
    assert total == expected["closing_balance_paise"]


def _alter_netbanking(tables: list[Any]) -> None:
    tables[1][3][6] = "1,17,600.00"  # page 2, the interest credit


def _alter_estatement(tables: list[Any]) -> None:
    tables[0][5][7] = "80100.00"  # page 1, the failed-UPI reversal


def _alter_yono(tables: list[Any]) -> None:
    tables[1][0][6] = "53,100.00"  # page 2, the interest credit


@pytest.mark.parametrize(
    ("layout", "alter", "page", "row"),
    [
        ("netbanking", _alter_netbanking, 2, 1),
        ("estatement", _alter_estatement, 1, 3),
        ("yono", _alter_yono, 2, 1),
    ],
)
def test_running_balance_mismatch_raises(
    layout: str,
    alter: Callable[[list[Any]], None],
    page: int,
    row: int,
    sbi_bank_fixture: _Fixture,
) -> None:
    """ADR-0010 rule 11: a row whose balance does not follow raises, naming page and row."""
    tables = copy.deepcopy(sbi_bank_fixture(f"tables_{layout}.json"))
    alter(tables)
    with pytest.raises(RunningBalanceMismatchError) as info:
        SbiBank.interpret_tables(tables)
    assert (info.value.page, info.value.row) == (page, row)
    assert str(info.value) == f"running balance does not match on page {page}, row {row}"


def test_failed_upi_reversal_is_a_positive_spend(sbi_bank_fixture: _Fixture) -> None:
    """ADR-0010 rule 12: the returned debit nets the debit it reverses (ADR-0009)."""
    rows = SbiBank.interpret_tables(sbi_bank_fixture("tables_estatement.json"))
    debit, reversal = rows[1], rows[2]
    assert (debit.amount_paise, debit.txn_type) == (-49900, "withdrawal")
    assert (reversal.amount_paise, reversal.txn_type) == (49900, "refund")
    assert _map_type(debit) == "spend"
    assert _map_type(reversal) == "spend"
    assert debit.amount_paise + reversal.amount_paise == 0


def test_card_bill_debit_is_a_payment_candidate(sbi_bank_fixture: _Fixture) -> None:
    """ADR-0010 rule 12: a debit naming a card-bill payment is flagged for F4a review."""
    rows = SbiBank.interpret_tables(sbi_bank_fixture("tables_estatement.json"))
    bill = rows[3]
    assert (bill.amount_paise, bill.txn_type) == (-1234567, "withdrawal")
    assert is_card_bill_debit(normalize_merchant(bill.merchant_raw))
    assert not any(
        is_card_bill_debit(normalize_merchant(r.merchant_raw)) for r in rows if r is not bill
    )


def test_estatement_maps_credit_and_debit_by_label() -> None:
    credit_first = [
        _ES_HEADER,
        ["02-07-26", "A", "", "", "-", "600.00", "-", "1600.00"],
        ["03-07-26", "B", "", "", "-", "-", "100.00", "1500.00"],
    ]
    debit_first = [
        ["Date", "Transaction Reference", "", "", "Ref.No./Chq.No.", "Debit", "Credit", "Balance"],
        ["02-07-26", "A", "", "", "-", "-", "600.00", "1600.00"],
        ["03-07-26", "B", "", "", "-", "100.00", "-", "1500.00"],
    ]
    a = SbiBank.interpret_tables([credit_first])
    b = SbiBank.interpret_tables([debit_first])
    assert _flat(a) == _flat(b) == [(60000, "deposit"), (-10000, "withdrawal")]


def test_estatement_dash_and_zero_cells_are_empty() -> None:
    table = [
        _ES_HEADER,
        ["02-07-26", "A", "", "", "-", "-", "499.00", "99501.00"],
        ["03-07-26", "B", "", "", "-", "0", "499.00", "99002.00"],
    ]
    assert _flat(SbiBank.interpret_tables([table])) == [(-49900, "withdrawal")] * 2


def _read_or_error(tables: list[list[list[str]]]) -> list[tuple[int, str]] | str:
    """The ``(amount, type)`` rows, or the error text, so a wrongly raised error fails an assert."""
    try:
        return _flat(SbiBank.interpret_tables(tables))
    except ParserError as e:
        return f"ParserError: {e}"


def test_reader_rejects_a_second_amount_in_one_column() -> None:
    table = [
        _ES_HEADER,
        ["02-07-26", "A", "", "", "-", "-", "499.00", "99501.00"],
        ["", "B", "", "", "-", "-", "100.00", ""],
    ]
    with pytest.raises(ParserError, match=r"cannot read row on page 1, row 1"):
        SbiBank.interpret_tables([table])


def test_estatement_padded_dash_cell_is_empty() -> None:
    table = [_ES_HEADER, ["02-07-26", "A", "", "", "-", " - ", "499.00", "99501.00"]]
    assert _read_or_error([table]) == [(-49900, "withdrawal")]


def test_yono_debit_and_credit_sit_left_of_balance() -> None:
    table = [
        ["", "", "", "", "", "", "Balance"],
        ["01/06/2026", "01/06/2026", "A", "-", "850.00", "-", "9,150.00"],
        ["02/06/2026", "02/06/2026", "B", "-", "-", "120.00", "9,270.00"],
    ]
    assert _flat(SbiBank.interpret_tables([table])) == [(-85000, "withdrawal"), (12000, "deposit")]


def test_yono_narration_drops_type_prefix_and_branch_stamp() -> None:
    raw = (
        "WDL TFR UPI/DR/000000000002/SENTINEL GROCER/EXAM/sentinelgrocer@example/UPI "
        "0000000000002 AT 00001 PBB SENTINEL BRANCH, CITY"
    )
    assert (
        sbi_bank._clean_yono_narration(raw)
        == "UPI/DR/000000000002/SENTINEL GROCER/EXAM/sentinelgrocer@example/UPI"
    )


def test_wrapped_narration_glues_at_separators() -> None:
    parts = [
        "TO TRANSFER-",
        "UPI/DR/000000000001/SENTINEL GROCER/",
        "EXAM/sentinelgrocer@example/UPI",
    ]
    assert sbi_bank._join_wrapped(parts) == (
        "TO TRANSFER-UPI/DR/000000000001/SENTINEL GROCER/EXAM/sentinelgrocer@example/UPI"
    )
    assert sbi_bank._join_wrapped(["SENTINEL", "FOOD"]) == "SENTINEL FOOD"


def test_netbanking_ruled_header_and_cell_dates() -> None:
    table = [
        [
            "Txn\nDate",
            "Value\nDate",
            "Description",
            "Ref No./Cheque\nNo.",
            "Debit",
            "Credit",
            "Balance",
        ],
        ["1 Apr\n2026", "1 Apr\n2026", "TO TRANSFER-\nINB SENTINEL", "", "100.00", "", "900.00"],
        ["2 Apr\n2026", "2 Apr\n2026", "BY TRANSFER-\nNEFT*SENTINEL", "", "", "50.00", "950.00"],
    ]
    rows = SbiBank.interpret_tables([table])
    assert [(r.date, r.amount_paise, r.merchant_raw) for r in rows] == [
        (date(2026, 4, 1), -10000, "TO TRANSFER-INB SENTINEL"),
        (date(2026, 4, 2), 5000, "BY TRANSFER-NEFT*SENTINEL"),
    ]


def test_netbanking_header_keeps_a_yearless_row_open() -> None:
    """D3: page 2 repeats the header above the year line of a row that began on page 1."""
    page1 = [
        _NB_HEADER,
        ["15 Apr", "15 Apr", "TO TRANSFER-", "", "5,000.00", "", "95,000.00"],
    ]
    page2 = [
        _NB_HEADER,
        ["2026", "2026", "INB SENTINEL", "", "", "", ""],
        ["16 Apr", "16 Apr", "BY TRANSFER-", "", "", "100.00", "95,100.00"],
        ["2026", "2026", "NEFT*SENTINEL", "", "", "", ""],
    ]
    rows = SbiBank.interpret_tables([page1, page2])
    assert [(r.date, r.amount_paise, r.merchant_raw) for r in rows] == [
        (date(2026, 4, 15), -500000, "TO TRANSFER-INB SENTINEL"),
        (date(2026, 4, 16), 10000, "BY TRANSFER-NEFT*SENTINEL"),
    ]


def test_netbanking_row_without_a_year_raises() -> None:
    table = [
        _NB_HEADER,
        ["15 Apr", "15 Apr", "X", "", "1.00", "", "9.00"],
        ["MR TEST USER", "", "", "", "", "", ""],
    ]
    with pytest.raises(ParserError, match=r"cannot read row on page 1, row 1"):
        SbiBank.interpret_tables([table])


@pytest.mark.parametrize(("debit", "credit"), [("", ""), ("100.00", "5.00")])
def test_reader_needs_exactly_one_amount(debit: str, credit: str) -> None:
    table = [_NB_HEADER, ["1 Apr 2026", "1 Apr", "X", "", debit, credit, "9.00"]]
    with pytest.raises(ParserError, match=r"(cannot read row|both a withdrawal).*page 1, row 1"):
        SbiBank.interpret_tables([table])


def test_interpret_tables_without_a_header_raises() -> None:
    """ADR-0010 rule 5: a reader that never found its header refuses."""
    with pytest.raises(ParserError, match="cannot find the transaction table"):
        SbiBank.interpret_tables([[["02-07-26", "X", "", "", "-", "1.00", "-", "1.00"]]])


def test_header_without_rows_is_a_zero_row_statement() -> None:
    assert SbiBank.interpret_tables([[_ES_HEADER]]) == []


def test_combined_account_sections_are_refused() -> None:
    combined = [
        "TRANSACTION DETAILS",
        "DL/TL ACCOUNT",
        "XXXXXXX0001",
        "TRANSACTION DETAILS",
        "SAVING ACCOUNT",
        "XXXXXXX0000",
    ]
    with pytest.raises(ParserError, match="more than one account section"):
        sbi_bank._check_account_sections(combined)
    with pytest.raises(ParserError, match="no savings account section"):
        sbi_bank._check_account_sections(combined[:3])


def test_one_savings_section_repeated_per_page_is_accepted() -> None:
    sbi_bank._check_account_sections(
        [
            "TRANSACTION DETAILS",
            "SAVING ACCOUNT",
            "XXXXXXX0000",
            "x",
            "TRANSACTION DETAILS",
            "SAVING ACCOUNT",
            "XXXXXXX0000",
        ]
    )
    sbi_bank._check_account_sections(["no marker here"])


def test_interpret_summary_estatement_reads_a_garbled_opening_label() -> None:
    lines = [
        "Yourn Oupllening nBualllance on 01-07-26: 20000.00",
        "Your Closing Balance on 31-07-26: 65804.33",
    ]
    assert SbiBank.interpret_summary(lines) == StatementSummary(
        2000000, 6580433, date(2026, 7, 1), date(2026, 7, 31)
    )


def test_interpret_summary_yono_cr_is_held_and_dr_is_overdrawn() -> None:
    label = (
        "Brought Forward( ) Dr Count Cr Count Total Debits( ) Total Credits( ) Closing Balance( )"
    )
    held = SbiBank.interpret_summary([label, "10,000.00 2 2 2,050.00 45,120.00 53,070.00CR"])
    assert held.closing_balance_paise == 5307000
    overdrawn = SbiBank.interpret_summary([label, "500.00DR 0 0 0.00 0.00 500.00DR"])
    assert (overdrawn.opening_balance_paise, overdrawn.closing_balance_paise) == (-50000, -50000)


def test_interpret_summary_netbanking_closing_is_the_last_printed_balance() -> None:
    lines = [
        "Balance as on 1 Apr 2026 : 100.00",
        "1 Apr 1 Apr TO TRANSFER- 10.00 90.00",
        "2026 2026 SENTINEL",
        "2 Apr 2 Apr BY TRANSFER- - 5.00 95.00",
    ]
    summary = SbiBank.interpret_summary(lines)
    assert (summary.opening_balance_paise, summary.closing_balance_paise) == (10000, 9500)


def test_interpret_summary_netbanking_without_rows_closes_at_the_opening() -> None:
    lines = [
        "Balance as on 1 Apr 2026 : 100.00",
        "Account Statement from 1 Apr 2026 to 30 Apr 2026",
    ]
    assert SbiBank.interpret_summary(lines) == StatementSummary(
        10000, 10000, date(2026, 4, 1), date(2026, 4, 30)
    )


def test_interpret_summary_no_labels_returns_all_none() -> None:
    assert SbiBank.interpret_summary(["nothing", "to see here"]) == StatementSummary()


_PositionedPdf = Callable[[list[list[tuple[float, float, str]]]], bytes]

_NB_SUMMARY_LINES = [
    (30, 760, "Balance as on 1 Apr 2026 : 2,00,000.00"),
    (30, 746, "Account Statement from 1 Apr 2026 to 30 Apr 2026"),
]
_NB_HEADER_WORDS = [
    (30, 700, "Txn Date"),
    (80, 700, "Value"),
    (130, 700, "Description"),
    (300, 700, "Ref No./Cheque"),
    (380, 700, "Debit"),
    (440, 700, "Credit"),
    (500, 700, "Balance"),
    (80, 690, "Date"),
    (300, 690, "No."),
]
_NB_ROW_WORDS = [
    (30, 676, "1 Apr"),
    (80, 676, "1 Apr"),
    (130, 676, "TO TRANSFER-"),
    (370, 676, "1,250.00"),
    (490, 676, "1,98,750.00"),
    (30, 664, "2026"),
    (80, 664, "2026"),
    (130, 664, "UPI/DR/000000000001/SENTINEL"),
    (30, 650, "2 Apr"),
    (80, 650, "2 Apr"),
    (130, 650, "BY TRANSFER-"),
    (430, 650, "75,000.00"),
    (490, 650, "2,73,750.00"),
    (30, 638, "2026"),
    (80, 638, "2026"),
    (130, 638, "NEFT*SENTINEL"),
]
_NB_FOOTER = [(30, 600, "**This is a computer generated statement.")]


def test_parse_netbanking_pdf_reads_rows_and_summary(positioned_pdf_bytes: _PositionedPdf) -> None:
    pdf = positioned_pdf_bytes([_NB_SUMMARY_LINES + _NB_HEADER_WORDS + _NB_ROW_WORDS + _NB_FOOTER])
    parsed = SbiBank.parse(pdf, password=None)
    assert [(r.date, r.amount_paise, r.txn_type) for r in parsed.rows] == [
        (date(2026, 4, 1), -125000, "withdrawal"),
        (date(2026, 4, 2), 7500000, "deposit"),
    ]
    assert parsed.summary == StatementSummary(
        20000000, 27375000, date(2026, 4, 1), date(2026, 4, 30)
    )


def test_parse_checks_the_first_row_against_the_opening(
    positioned_pdf_bytes: _PositionedPdf,
) -> None:
    summary = [(30, 760, "Balance as on 1 Apr 2026 : 2,01,000.00"), _NB_SUMMARY_LINES[1]]
    pdf = positioned_pdf_bytes([summary + _NB_HEADER_WORDS + _NB_ROW_WORDS + _NB_FOOTER])
    with pytest.raises(RunningBalanceMismatchError) as info:
        SbiBank.parse(pdf, password=None)
    assert (info.value.page, info.value.row) == (1, 1)


def test_parse_netbanking_header_without_rows_imports_zero_rows(
    positioned_pdf_bytes: _PositionedPdf,
) -> None:
    pdf = positioned_pdf_bytes([_NB_SUMMARY_LINES + _NB_HEADER_WORDS + _NB_FOOTER])
    parsed = SbiBank.parse(pdf, password=None)
    assert parsed.rows == []
    assert parsed.summary == StatementSummary(
        20000000, 20000000, date(2026, 4, 1), date(2026, 4, 30)
    )


def test_parse_refuses_a_combined_estatement(
    monkeypatch: pytest.MonkeyPatch, positioned_pdf_bytes: _PositionedPdf
) -> None:
    lines = [
        "TRANSACTION DETAILS",
        "DL/TL ACCOUNT",
        "XXXXXXX0001",
        "Your Opening Balance on 01-07-26: 510000.00",
        "Your Closing Balance on 31-07-26: 500000.00",
        "TRANSACTION DETAILS",
        "SAVING ACCOUNT",
        "XXXXXXX0000",
        "Your Opening Balance on 01-07-26: 20000.00",
        "Your Closing Balance on 31-07-26: 80000.00",
    ]
    tables = [
        [
            _ES_HEADER,
            [
                "01-07-26",
                "PRINCIPAL REPAYMENT GL TO LOAN",
                "",
                "",
                "-",
                "10000.00",
                "-",
                "500000.00",
            ],
        ],
        [
            _ES_HEADER,
            [
                "02-07-26",
                "NEFT*EXAM0000001*SENTINEL EMPLOYER",
                "",
                "",
                "-",
                "60000.00",
                "0",
                "80000.00",
            ],
        ],
    ]
    monkeypatch.setattr(sbi_bank, "_extract_text", lambda _pdf: lines)
    monkeypatch.setattr(sbi_bank, "_extract_tables", lambda _pdf: tables)
    with pytest.raises(ParserError, match="more than one account section"):
        SbiBank.parse(positioned_pdf_bytes([[], []]), password=None)


def test_parse_refuses_an_empty_estatement_month(
    monkeypatch: pytest.MonkeyPatch, positioned_pdf_bytes: _PositionedPdf
) -> None:
    lines = [
        "TRANSACTION DETAILS",
        "SAVING ACCOUNT",
        "XXXXXXX0000",
        "Available Balance 35481.14",
        "As on 31-05-26",
    ]
    monkeypatch.setattr(sbi_bank, "_extract_text", lambda _pdf: lines)
    monkeypatch.setattr(sbi_bank, "_extract_tables", lambda _pdf: [[]])
    with pytest.raises(ParserError, match="cannot find the transaction table"):
        SbiBank.parse(positioned_pdf_bytes([[]]), password=None)


def test_parse_corrupt_bytes_raises() -> None:
    with pytest.raises(ParserError):
        SbiBank.parse(b"this is definitely not a pdf", password=None)


def test_parse_wrong_password_raises(encrypted_pdf_bytes: bytes) -> None:
    with pytest.raises(InvalidPasswordError):
        SbiBank.parse(encrypted_pdf_bytes, password="definitely-not-the-right-password-xyz")


def test_sbi_bank_is_registered_for_bank_accounts() -> None:
    assert import_service.statement_import_supported("bank", "sbi")
    assert import_service.PARSERS[("sbi", "bank")] is SbiBank
    assert import_service.ISSUER_LABELS["sbi"] == "SBI"


def _shape(pdf: bytes, password: str) -> str:
    """A PII-free account of the extracted tables, for a failure message: counts only."""
    pages = _extract_tables(_decrypt(pdf, password))
    parts = []
    for i, rows in enumerate(pages, start=1):
        widths = sorted({len(r) for r in rows})
        parts.append(f"page {i}: {len(rows)} rows, cell counts {widths}")
    return "; ".join(parts)


@pytest.mark.real_pdf
def test_parse_real_pdf_smoke(sbi_bank_real_pdf: bytes, sbi_bank_real_password: str) -> None:
    """End to end on the user's own statement. Failure text carries integers and counts only."""
    try:
        parsed = SbiBank.parse(sbi_bank_real_pdf, password=sbi_bank_real_password)
    except RunningBalanceMismatchError as e:
        pytest.fail(f"running balance mismatch on page {e.page}, row {e.row}")
    except ParserError as e:
        pytest.fail(
            f"parser error ({type(e).__name__}): {e}; "
            f"{_shape(sbi_bank_real_pdf, sbi_bank_real_password)}"
        )
    summary = parsed.summary
    assert summary.opening_balance_paise is not None, "no opening balance read"
    assert summary.closing_balance_paise is not None, "no closing balance read"
    assert summary.period_start is not None and summary.period_end is not None, "no period read"
    total = summary.opening_balance_paise + sum(r.amount_paise for r in parsed.rows)
    assert total == summary.closing_balance_paise, (
        f"opening + rows differs from closing by {total - summary.closing_balance_paise} paise"
    )
