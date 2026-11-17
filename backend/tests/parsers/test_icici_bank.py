"""Tests for :class:`IciciBank`, over synthetic fixtures for each known layout.

* ``estatement``: the current e-statement (``PARTICULARS`` column, narration centred on the
  date line, a bare counterparty-name line first on UPI/IMPS/NEFT rows).
* ``pre2020``: the pre-2020 statement (no ``PARTICULARS`` label, narration in the MODE
  column, a row starts at its date line and continues downward).

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

from app.parsers import IciciBank, InvalidPasswordError, ParserError, RunningBalanceMismatchError
from app.parsers.base import StatementSummary, _decrypt, _extract_word_table
from app.parsers.icici_bank import _LABELS, _LEGACY_LABELS, _STOP_RE, _header_columns
from app.services import import_service
from app.services.import_service import _map_type
from app.services.merchant import normalize_merchant
from app.services.reconciliation_service import is_card_bill_debit

LAYOUTS = ["estatement", "pre2020"]

_Fixture = Callable[[str], Any]

_H = ["DATE", "MODE", "PARTICULARS", "DEPOSITS", "WITHDRAWALS", "BALANCE"]
_LEGACY_H = ["DATE", "MODE", "DEPOSITS", "WITHDRAWALS", "BALANCE"]


def _serialize(rows: list[Any]) -> list[dict[str, Any]]:
    return [{**asdict(r), "date": r.date.isoformat()} for r in rows]


def _bf(balance: str, day: str = "01-04-2026") -> list[str]:
    return [day, "", "B/F", "", "", balance]


def _line(text: str) -> list[str]:
    """A particulars-only line of the current layout."""
    return ["", "", text, "", "", ""]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_interpret_tables_snapshot(layout: str, icici_bank_fixture: _Fixture) -> None:
    rows = IciciBank.interpret_tables(icici_bank_fixture(f"tables_{layout}.json"))
    assert _serialize(rows) == icici_bank_fixture(f"tables_{layout}.expected.json")


@pytest.mark.parametrize("layout", LAYOUTS)
def test_interpret_summary_snapshot(layout: str, icici_bank_fixture: _Fixture) -> None:
    summary = IciciBank.interpret_summary(icici_bank_fixture(f"summary_{layout}.txt"))
    expected = icici_bank_fixture(f"summary_{layout}.expected.json")
    assert summary == StatementSummary(
        opening_balance_paise=expected["opening_balance_paise"],
        closing_balance_paise=expected["closing_balance_paise"],
        period_start=date.fromisoformat(expected["period_start"]),
        period_end=date.fromisoformat(expected["period_end"]),
    )


@pytest.mark.parametrize("layout", LAYOUTS)
def test_fixture_is_self_consistent(layout: str, icici_bank_fixture: _Fixture) -> None:
    """The fixtures are only evidence if they obey the statement's own arithmetic."""
    expected = icici_bank_fixture(f"summary_{layout}.expected.json")
    rows = icici_bank_fixture(f"tables_{layout}.expected.json")
    total = expected["opening_balance_paise"] + sum(r["amount_paise"] for r in rows)
    assert total == expected["closing_balance_paise"]


def _alter_estatement(tables: list[Any]) -> None:
    tables[1][1][5] = "2,09,455.33"  # page 2, transaction 1: the cashback deposit


def _alter_pre2020(tables: list[Any]) -> None:
    tables[0][5][4] = "73,260.00"  # page 1, transaction 3: the NEFT deposit


@pytest.mark.parametrize(
    ("layout", "alter", "page", "row"),
    [
        ("estatement", _alter_estatement, 2, 1),
        ("pre2020", _alter_pre2020, 1, 3),
    ],
)
def test_running_balance_mismatch_raises(
    layout: str,
    alter: Callable[[list[Any]], None],
    page: int,
    row: int,
    icici_bank_fixture: _Fixture,
) -> None:
    """ADR-0010 rule 11: a row whose balance does not follow raises, naming page and row."""
    tables = copy.deepcopy(icici_bank_fixture(f"tables_{layout}.json"))
    alter(tables)
    with pytest.raises(RunningBalanceMismatchError) as info:
        IciciBank.interpret_tables(tables)
    assert (info.value.page, info.value.row) == (page, row)
    assert str(info.value) == f"running balance does not match on page {page}, row {row}"


def test_first_row_is_checked_against_the_bf_row(icici_bank_fixture: _Fixture) -> None:
    """ADR-0010 rule 11 (as amended): ICICI prints the opening balance as a ``B/F`` row of
    the table, so ``interpret_tables`` alone checks row 1 against it."""
    tables = copy.deepcopy(icici_bank_fixture("tables_estatement.json"))
    tables[0][1][5] = "1,50,001.00"
    with pytest.raises(RunningBalanceMismatchError) as info:
        IciciBank.interpret_tables(tables)
    assert (info.value.page, info.value.row) == (1, 1)


def test_without_a_bf_row_the_check_starts_at_row_two() -> None:
    first = ["01-04-2026", "", "SENTINEL ONE", "", "100.00", "900.00"]
    bad = ["02-04-2026", "", "SENTINEL TWO", "", "100.00", "850.00"]
    good = ["02-04-2026", "", "SENTINEL TWO", "", "100.00", "800.00"]
    with pytest.raises(RunningBalanceMismatchError) as info:
        IciciBank.interpret_tables([[_H, first, bad]])
    assert (info.value.page, info.value.row) == (1, 2)
    assert len(IciciBank.interpret_tables([[_H, first, good]])) == 2


def test_failed_upi_reversal_is_a_positive_spend(icici_bank_fixture: _Fixture) -> None:
    """ADR-0010 rule 12: the returned debit nets the debit it reverses (ADR-0009)."""
    rows = IciciBank.interpret_tables(icici_bank_fixture("tables_estatement.json"))
    debit, reversal = rows[3], rows[4]
    assert (debit.amount_paise, debit.txn_type) == (-49900, "withdrawal")
    assert (reversal.amount_paise, reversal.txn_type) == (49900, "refund")
    assert _map_type(debit) == "spend"
    assert _map_type(reversal) == "spend"
    assert debit.amount_paise + reversal.amount_paise == 0


def test_card_bill_debit_is_a_payment_candidate(icici_bank_fixture: _Fixture) -> None:
    """ADR-0010 rule 12: a debit naming a card-bill payment is flagged for F4a review."""
    rows = IciciBank.interpret_tables(icici_bank_fixture("tables_estatement.json"))
    bill = rows[5]
    assert (bill.amount_paise, bill.txn_type) == (-1234567, "withdrawal")
    assert is_card_bill_debit(normalize_merchant(bill.merchant_raw))
    assert not any(
        is_card_bill_debit(normalize_merchant(r.merchant_raw)) for r in rows if r is not bill
    )


def test_cashback_deposit_imports_as_a_positive_spend(icici_bank_fixture: _Fixture) -> None:
    rows = IciciBank.interpret_tables(icici_bank_fixture("tables_estatement.json"))
    assert (rows[6].txn_type, _map_type(rows[6])) == ("deposit", "spend")
    assert (rows[1].txn_type, _map_type(rows[1])) == ("deposit", "income")


def test_deposits_print_before_withdrawals() -> None:
    tables = [[_H, _bf("1,000.00"), ["02-04-2026", "", "SENTINEL", "100.00", "", "1,100.00"]]]
    rows = IciciBank.interpret_tables(tables)
    assert [(r.amount_paise, r.txn_type) for r in rows] == [(10000, "deposit")]


def test_centred_narration_splits_lines_by_symmetry() -> None:
    """A row takes as many particulars lines below its date line as it has above it."""
    d13 = "13-07-2026"
    tables = [
        [
            _H,
            _bf("1,000.00", d13),
            _line("SENTINEL PERSON"),
            [d13, "MOBILE BANKING", "", "1,200.00", "", "2,200.00"],
            _line("MMT/IMPS/000000000021/Self transfer/SENTINEL PERSON/DEMO Bank"),
            _line("SENTINEL PERSON"),
            [d13, "MOBILE BANKING", "", "800.00", "", "3,000.00"],
            _line("MMT/IMPS/000000000022/Test/SENTINEL PERSON/DEMO Bank"),
            [d13, "", "BIL/INFT/TEST000001/ SENTINEL PERSON", "500.00", "", "3,500.00"],
            _line("OTHER SENTINEL"),
            _line("UPI/OTHER SENTINEL/other@example/Paid via C/DEMO"),
            ["14-07-2026", "", "", "100.00", "", "3,600.00"],
            _line("BANK/000000000023/DEMO0000"),
            _line("3"),
            [
                "15-07-2026",
                "ICICI CRM",
                "CAM/00000AAA/CASH DEP-Other/15-07-26/0000",
                "2,500.00",
                "",
                "6,100.00",
            ],
            _line("DEMO BROKING LIMITED"),
            [
                "16-07-2026",
                "MOBILE BANKING",
                "MMT/IMPS/000000000024/DEMO BR/Yes",
                "300.00",
                "",
                "6,400.00",
            ],
            _line("Bank"),
        ]
    ]
    rows = IciciBank.interpret_tables(tables)
    assert [r.merchant_raw for r in rows] == [
        "MOBILE BANKING SENTINEL PERSON "
        "MMT/IMPS/000000000021/Self transfer/SENTINEL PERSON/DEMO Bank",
        "MOBILE BANKING SENTINEL PERSON MMT/IMPS/000000000022/Test/SENTINEL PERSON/DEMO Bank",
        "BIL/INFT/TEST000001/ SENTINEL PERSON",
        "OTHER SENTINEL UPI/OTHER SENTINEL/other@example/Paid via C/DEMO "
        "BANK/000000000023/DEMO0000 3",
        "ICICI CRM CAM/00000AAA/CASH DEP-Other/15-07-26/0000",
        "MOBILE BANKING DEMO BROKING LIMITED MMT/IMPS/000000000024/DEMO BR/Yes Bank",
    ]


def test_bf_row_does_not_take_row_one_lines() -> None:
    """A ``B/F`` line owes nothing below it, so the line under it belongs to row 1 (D2)."""
    tables = [
        [
            _H,
            _line("NOTE"),
            _bf("1,000.00"),
            _line("SENTINEL"),
            ["02-04-2026", "", "SHOP", "", "1.00", "999.00"],
        ]
    ]
    rows = IciciBank.interpret_tables(tables)
    assert [r.merchant_raw for r in rows] == ["SENTINEL SHOP"]


def test_narration_spilled_into_an_amount_column_returns_to_the_narration() -> None:
    tables = [
        [
            _H,
            _bf("1,000.00"),
            [
                "02-04-2026",
                "",
                "UPI/SENTINEL/sentinel@example/Payment",
                "fr/AXIS",
                "100.00",
                "900.00",
            ],
            ["03-04-2026", "", "NEFT-SENTINEL", "EMPLOYER 50.00", "", "950.00"],
        ]
    ]
    rows = IciciBank.interpret_tables(tables)
    assert [(r.merchant_raw, r.amount_paise) for r in rows] == [
        ("UPI/SENTINEL/sentinel@example/Payment fr/AXIS", -10000),
        ("NEFT-SENTINEL EMPLOYER", 5000),
    ]


@pytest.mark.parametrize(("deposit", "withdrawal"), [("", ""), ("100.00", "5.00")])
def test_date_line_needs_exactly_one_amount(deposit: str, withdrawal: str) -> None:
    tables = [[_H, _bf("1,000.00"), ["02-04-2026", "", "SENTINEL", deposit, withdrawal, "900.00"]]]
    with pytest.raises(ParserError, match=r"cannot read row on page 1, row 1"):
        IciciBank.interpret_tables(tables)


def test_pre2020_rejects_a_second_amount_on_one_row() -> None:
    tables = [
        [
            _LEGACY_H,
            ["01-04-2019", "SENTINEL", "", "100.00", "900.00"],
            ["", "MORE", "", "", "800.00"],
        ]
    ]
    with pytest.raises(ParserError, match=r"cannot read row on page 1, row 1"):
        IciciBank.interpret_tables(tables)


def _read_or_error(tables: list[list[list[str]]]) -> list[tuple[str, int]] | str:
    """The ``(merchant_raw, amount)`` rows, or the error text, so a wrongly raised error fails."""
    try:
        rows = IciciBank.interpret_tables(tables)
    except ParserError as e:
        return f"ParserError: {e}"
    return [(r.merchant_raw, r.amount_paise) for r in rows]


def test_pre2020_row_continues_across_a_page_break() -> None:
    tables = [
        [
            _LEGACY_H,
            ["01-04-2019", "B/F", "", "", "1,000.00"],
            ["02-04-2019", "UPI/000000000011/SENTINEL", "", "100.00", "900.00"],
        ],
        [
            _LEGACY_H,
            ["", "SHOP", "", "", ""],
            ["03-04-2019", "NEFT-SENTINEL", "50.00", "", "950.00"],
        ],
    ]
    assert _read_or_error(tables) == [
        ("UPI/000000000011/SENTINEL SHOP", -10000),
        ("NEFT-SENTINEL", 5000),
    ]


def test_pre2020_amounts_on_the_next_page_complete_the_row() -> None:
    tables = [
        [
            _LEGACY_H,
            ["01-04-2019", "B/F", "", "", "1,000.00"],
            ["02-04-2019", "UPI/000000000011/SENTINEL", "", "", ""],
        ],
        [_LEGACY_H, ["", "SHOP", "", "100.00", "900.00"]],
    ]
    assert _read_or_error(tables) == [("UPI/000000000011/SENTINEL SHOP", -10000)]


def test_current_layout_does_not_carry_across_a_page_break() -> None:
    """Scope pin: a centred row is read within its page, so a page-end footer stays out of
    the next page's first narration."""
    tables = [
        [
            _H,
            _bf("1,000.00"),
            ["02-04-2026", "", "SENTINEL ONE", "", "100.00", "900.00"],
            _line("Page 1 of 2"),
        ],
        [_H, ["03-04-2026", "", "SENTINEL TWO", "", "50.00", "850.00"]],
    ]
    rows = IciciBank.interpret_tables(tables)
    assert [r.merchant_raw for r in rows] == ["SENTINEL ONE", "SENTINEL TWO"]


def test_only_the_first_bf_row_opens() -> None:
    """The e-statement repeats ``B/F`` after a page break; it is skipped, not a new opening."""
    tables = [
        [
            _H,
            _bf("1,000.00"),
            ["02-04-2026", "", "SENTINEL ONE", "", "100.00", "900.00"],
        ],
        [
            _H,
            _bf("900.00", "02-04-2026"),
            ["03-04-2026", "", "SENTINEL TWO", "", "50.00", "850.00"],
        ],
    ]
    rows = IciciBank.interpret_tables(tables)
    assert [r.amount_paise for r in rows] == [-10000, -5000]


def test_no_table_header_raises() -> None:
    """ADR-0010 rule 5: a reader that never finds its header refuses the file."""
    with pytest.raises(ParserError, match=r"cannot find the transaction table"):
        IciciBank.interpret_tables([[["01-04-2026", "", "B/F", "", "", "1.00"]]])


def test_interpret_summary_reads_a_period_wrapped_over_two_lines() -> None:
    summary = IciciBank.interpret_summary(
        [
            "Statement of Transactions in Savings Account XXXXXXXX0000 in INR for the period",
            "April 01, 2026 - April 30, 2026",
        ]
    )
    assert (summary.period_start, summary.period_end) == (date(2026, 4, 1), date(2026, 4, 30))


def test_interpret_summary_stops_at_linked_fixed_deposits() -> None:
    summary = IciciBank.interpret_summary(
        [
            "DATE MODE PARTICULARS DEPOSITS WITHDRAWALS BALANCE",
            "01-04-2026 B/F 100.00",
            "02-04-2026 SENTINEL 10.00 90.00",
            "Statement of Linked Fixed Deposits",
            "01-04-2026 FD/000000000009 500.00 500.00",
        ]
    )
    assert (summary.opening_balance_paise, summary.closing_balance_paise) == (10000, 9000)


def test_interpret_summary_no_labels_returns_all_none() -> None:
    assert IciciBank.interpret_summary(["nothing", "to see here"]) == StatementSummary()


# ---------------------------------------------------------------------------
# parse(): the whole path over a synthetic PDF.
# ---------------------------------------------------------------------------

_Pages = list[list[tuple[float, float, str]]]

_TITLE = (
    30,
    760,
    "Statement of Transactions in Savings Account XXXXXXXX0000 in INR for the period "
    "April 01, 2026 - April 30, 2026",
)
_HEADER = [
    (30, 730, "DATE"),
    (71, 730, "MODE"),
    (136, 730, "PARTICULARS"),
    (380, 730, "DEPOSITS"),
    (450, 730, "WITHDRAWALS"),
    (535, 730, "BALANCE"),
]


def _estatement_page() -> _Pages:
    page: list[tuple[float, float, str]] = [_TITLE, *_HEADER]
    page += [
        (30, 716, "01-04-2026"),
        (136, 716, "B/F"),
        (530, 716, "1,50,000.00"),
        # Row 1: one line above its date line and one below. "via" sits left of the DEPOSITS
        # boundary's reach, so the column split puts it in the amount column.
        (136, 704.47, "SENTINEL GROCER"),
        (30, 700, "02-04-2026"),
        (136, 700, "UPI/SENTINEL GROCER/sentinelgrocer@example/Paid"),
        (345, 700, "via"),
        (455, 700, "1,250.00"),
        (530, 700, "1,48,750.00"),
        (136, 695.53, "AXIS BANK/000000000001/"),
        # Row 2.
        (136, 684.24, "SENTINEL EMPLOYER"),
        (30, 680, "03-04-2026"),
        (380, 680, "75,000.00"),
        (530, 680, "2,23,750.00"),
        (136, 675.76, "NEFT-EXAMN00000000001-SENTINEL EMPLOYER"),
        (137, 660, "Total:"),
        (380, 660, "75,000.00"),
        (455, 660, "1,250.00"),
        (530, 660, "2,23,750.00"),
        (30, 640, "Account Related Other Information"),
    ]
    return [page]


def test_parse_estatement_pdf_reads_rows_and_summary(
    positioned_pdf_bytes: Callable[[_Pages], bytes],
) -> None:
    pdf = positioned_pdf_bytes(_estatement_page())
    # Precondition: the shared column split really does put "via" in the DEPOSITS cell,
    # so the spill repair is what rescues the narration.
    tables = _extract_word_table(
        _decrypt(pdf, None), headers=(_LABELS, _LEGACY_LABELS), stop_re=_STOP_RE
    )
    date_row = next(r for r in tables[0] if r[0] == "02-04-2026")
    assert "via" in date_row[3]

    parsed = IciciBank.parse(pdf, password=None)
    assert [(r.date, r.amount_paise, r.txn_type, r.merchant_raw) for r in parsed.rows] == [
        (
            date(2026, 4, 2),
            -125000,
            "withdrawal",
            "SENTINEL GROCER UPI/SENTINEL GROCER/sentinelgrocer@example/Paid via "
            "AXIS BANK/000000000001/",
        ),
        (
            date(2026, 4, 3),
            7500000,
            "deposit",
            "SENTINEL EMPLOYER NEFT-EXAMN00000000001-SENTINEL EMPLOYER",
        ),
    ]
    assert parsed.summary == StatementSummary(
        15000000, 22375000, date(2026, 4, 1), date(2026, 4, 30)
    )


def test_parse_reads_a_zero_transaction_statement(
    positioned_pdf_bytes: Callable[[_Pages], bytes],
) -> None:
    """ADR-0010 rule 5: a header with no rows under it is a valid empty statement."""
    page: list[tuple[float, float, str]] = [
        _TITLE,
        *_HEADER,
        (30, 716, "01-04-2026"),
        (136, 716, "B/F"),
        (530, 716, "1,50,000.00"),
        (137, 700, "Total:"),
    ]
    parsed = IciciBank.parse(positioned_pdf_bytes([page]), password=None)
    assert parsed.rows == []
    assert parsed.summary == StatementSummary(
        15000000, 15000000, date(2026, 4, 1), date(2026, 4, 30)
    )


def test_parse_without_a_table_header_raises(
    text_pdf_bytes: Callable[[list[str]], bytes],
) -> None:
    """The netbanking Detailed Statement export has no ``DATE MODE ...`` header: refused."""
    pdf = text_pdf_bytes(
        [
            "Statement of Transactions in Savings Account XXXXXXXX0000 in INR for the period "
            "April 01, 2026 - April 30, 2026",
            "S No. Transaction Remarks Withdrawal Amount (INR)",
        ]
    )
    with pytest.raises(ParserError, match=r"cannot find the transaction table"):
        IciciBank.parse(pdf, password=None)


def test_parse_wrong_password_raises(encrypted_pdf_bytes: bytes) -> None:
    with pytest.raises(InvalidPasswordError):
        IciciBank.parse(encrypted_pdf_bytes, password="definitely-not-the-right-password-xyz")


def test_icici_savings_accounts_are_importable() -> None:
    assert import_service.statement_import_supported("bank", "icici")
    assert import_service.PARSERS[("icici", "bank")] is IciciBank


def _shape(pdf: bytes, password: str) -> str:
    """A PII-free account of the extracted tables, for a failure message: counts and which
    header variant matched on each page, never a cell's content."""
    pages = _extract_word_table(
        _decrypt(pdf, password), headers=(_LABELS, _LEGACY_LABELS), stop_re=_STOP_RE
    )
    parts = []
    for i, rows in enumerate(pages, start=1):
        widths = sorted({len(r) for r in rows})
        header = _header_columns(rows[0]) if rows else None
        variant = "none" if header is None else ("current" if header.centred else "pre2020")
        parts.append(f"page {i}: {len(rows)} rows, cell counts {widths}, header {variant}")
    return "; ".join(parts)


@pytest.mark.real_pdf
def test_parse_real_pdf_smoke(icici_bank_real_pdf: bytes, icici_bank_real_password: str) -> None:
    """End to end on the user's own statement. Failure text carries integers and counts only."""
    try:
        parsed = IciciBank.parse(icici_bank_real_pdf, password=icici_bank_real_password)
    except RunningBalanceMismatchError as e:
        pytest.fail(
            f"running balance mismatch on page {e.page}, row {e.row}; "
            f"{_shape(icici_bank_real_pdf, icici_bank_real_password)}"
        )
    except ParserError as e:
        pytest.fail(
            f"parser error ({type(e).__name__}): {e}; "
            f"{_shape(icici_bank_real_pdf, icici_bank_real_password)}"
        )
    summary = parsed.summary
    assert parsed.rows, "no rows read"
    assert summary.opening_balance_paise is not None, "no opening balance read"
    assert summary.closing_balance_paise is not None, "no closing balance read"
    assert summary.period_start is not None and summary.period_end is not None, "no period read"
    total = summary.opening_balance_paise + sum(r.amount_paise for r in parsed.rows)
    assert total == summary.closing_balance_paise, (
        f"opening + rows differs from closing by {total - summary.closing_balance_paise} paise; "
        f"{_shape(icici_bank_real_pdf, icici_bank_real_password)}"
    )
