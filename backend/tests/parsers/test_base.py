"""Invariant tests for :class:`RawTransaction`, plus the shared column detective.

The sign convention (purchase ≤ 0, payment/refund ≥ 0, other unconstrained)
is enforced at construction so a parser bug that flips a sign blows up at
the offending row rather than poisoning downstream reports silently.

``_interpret_row`` is the layout detective every statement parser routes through, so a
mis-picked column is a wrong number on a money path for every issuer at once.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date

import pytest

from app.parsers import RawTransaction
from app.parsers.base import (
    ParsedStatement,
    ParserError,
    RunningBalanceMismatchError,
    StatementSummary,
    _extract_text,
    _extract_word_table,
    _find_labelled_amount,
    _find_period,
    _interpret_row,
    parse_statement,
)
from app.parsers.icici_cc import IciciCC, _classify


def _tx(amount_paise: int, txn_type: str) -> RawTransaction:
    # a strict checker won't accept str → TxnType at the test layer; the tests
    # exist to prove the runtime invariant so str is fine here.
    return RawTransaction(
        date=date(2026, 3, 15),
        amount_paise=amount_paise,
        merchant_raw="SENTINEL",
        txn_type=txn_type,  # type: ignore[arg-type]
    )


def test_purchase_negative_paise_ok() -> None:
    assert _tx(-100, "purchase").amount_paise == -100


def test_purchase_zero_paise_ok() -> None:
    assert _tx(0, "purchase").amount_paise == 0


def test_purchase_positive_paise_raises() -> None:
    with pytest.raises(ValueError, match=r"purchase must have amount_paise <= 0"):
        _tx(100, "purchase")


def test_withdrawal_negative_paise_ok() -> None:
    assert _tx(-100, "withdrawal").amount_paise == -100


def test_withdrawal_positive_paise_raises() -> None:
    with pytest.raises(ValueError, match=r"withdrawal must have amount_paise <= 0"):
        _tx(100, "withdrawal")


def test_deposit_positive_paise_ok() -> None:
    assert _tx(100, "deposit").amount_paise == 100


def test_deposit_negative_paise_raises() -> None:
    with pytest.raises(ValueError, match=r"deposit must have amount_paise >= 0"):
        _tx(-100, "deposit")


def test_running_balance_mismatch_error_carries_page_and_row() -> None:
    err = RunningBalanceMismatchError(page=2, row=3)
    assert isinstance(err, ParserError)
    assert (err.page, err.row) == (2, 3)
    assert str(err) == "running balance does not match on page 2, row 3"


def test_payment_positive_paise_ok() -> None:
    assert _tx(100, "payment").amount_paise == 100


def test_payment_zero_paise_ok() -> None:
    assert _tx(0, "payment").amount_paise == 0


def test_payment_negative_paise_raises() -> None:
    with pytest.raises(ValueError, match=r"payment must have amount_paise >= 0"):
        _tx(-100, "payment")


def test_refund_positive_paise_ok() -> None:
    assert _tx(100, "refund").amount_paise == 100


def test_refund_negative_paise_raises() -> None:
    with pytest.raises(ValueError, match=r"refund must have amount_paise >= 0"):
        _tx(-100, "refund")


def test_other_accepts_negative() -> None:
    assert _tx(-100, "other").amount_paise == -100


def test_other_accepts_positive() -> None:
    assert _tx(100, "other").amount_paise == 100


def test_leading_serial_column_is_not_read_as_the_amount() -> None:
    """B#18: the amount scan started at index 0, skipping only the date cell, so a leading
    serial-number column won it — ``_AMOUNT_RE`` matches a bare ``1``.

    Every row of such a statement imported as a 1-rupee purchase with the real amount joined
    into ``merchant_raw``, which poisons the F4 fingerprint, the F3 tag-map key and every
    spend total at once. Driven through the ICICI formats/classifier because that is the
    issuer whose statements carry the serial column.
    """
    txn = _interpret_row(
        ["1", "10/04/2026", "11/04/2026", "SENTINEL GROCERY MUM", "1,234.50"],
        IciciCC.DATE_FORMATS,
        _classify,
    )

    assert txn is not None
    assert txn.amount_paise == -123450  # debit, no CR marker → purchase, negative
    assert txn.date == date(2026, 4, 10)
    assert "1,234.50" not in txn.merchant_raw
    assert txn.merchant_raw == "1 SENTINEL GROCERY MUM"


def test_amount_after_date_is_still_found_without_a_serial_column() -> None:
    """The ordinary 4-column ICICI shape, pinned alongside the fix so the narrowed scan
    cannot pass by refusing to find an amount at all."""
    txn = _interpret_row(
        ["10/04/2026", "11/04/2026", "SENTINEL GROCERY MUM", "1,234.50"],
        IciciCC.DATE_FORMATS,
        _classify,
    )

    assert txn is not None
    assert txn.amount_paise == -123450
    assert txn.merchant_raw == "SENTINEL GROCERY MUM"


# --- ParsedStatement: the zero-row rule (ADR-0010 rule 5) ---


def test_parsed_statement_with_no_rows_and_a_summary_constructs() -> None:
    summary = StatementSummary(closing_balance_paise=0)

    parsed = ParsedStatement(rows=[], summary=summary)

    assert parsed.rows == []
    assert parsed.summary == summary


def test_parsed_statement_with_no_rows_and_an_all_none_summary_raises() -> None:
    with pytest.raises(ParserError, match="no transactions found"):
        ParsedStatement(rows=[], summary=StatementSummary())


# --- balance-reconciliation plan Phase 2: _extract_text / _find_labelled_amount / _find_period ---


def test_extract_text_raises_on_corrupt_bytes() -> None:
    with pytest.raises(ParserError, match="failed to extract text"):
        _extract_text(b"this is definitely not a pdf")


def test_extract_text_returns_a_list_of_lines_for_a_textless_pdf() -> None:
    """Mirrors _extract_tables's own contract: a well-formed PDF with nothing
    on the page returns an empty list, never raises and never None."""
    import io

    import pikepdf

    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    buf = io.BytesIO()
    pdf.save(buf)

    assert _extract_text(buf.getvalue()) == []


def test_find_labelled_amount_finds_the_trailing_value() -> None:
    lines = ["Statement Period: 01/03/2026 to 31/03/2026", "Previous Balance          5,699.00"]
    pattern = re.compile(r"Previous\s+Balance", re.IGNORECASE)

    assert _find_labelled_amount(lines, pattern) == (569900, False)


def test_find_labelled_amount_handles_a_credit_marker_split_by_whitespace() -> None:
    """ "1,234.56 CR" tokenises as two words — the two-word attempt must run
    before the one-word attempt, or the marker gets silently dropped."""
    lines = ["Total Amount Due          1,234.56 CR"]
    pattern = re.compile(r"Total\s+Amount\s+Due", re.IGNORECASE)

    assert _find_labelled_amount(lines, pattern) == (123456, True)


def test_find_labelled_amount_skips_an_unparseable_match_for_a_later_line() -> None:
    lines = ["Previous Balance carried forward", "Previous Balance          100.00"]
    pattern = re.compile(r"Previous\s+Balance", re.IGNORECASE)

    assert _find_labelled_amount(lines, pattern) == (10000, False)


def test_find_labelled_amount_returns_none_when_the_pattern_is_absent() -> None:
    lines = ["Total Amount Due          1,234.56"]
    pattern = re.compile(r"Statement\s+Period", re.IGNORECASE)

    assert _find_labelled_amount(lines, pattern) is None


def test_find_period_extracts_the_first_two_dates_on_the_matched_line() -> None:
    lines = ["Statement Period: 01/03/2026 to 31/03/2026"]
    pattern = re.compile(r"Statement\s+Period", re.IGNORECASE)

    result = _find_period(lines, pattern, ("%d/%m/%Y",))

    assert result == (date(2026, 3, 1), date(2026, 3, 31))


def test_find_period_returns_none_when_the_matched_line_has_only_one_date() -> None:
    lines = ["Payment Due Date 25/03/2026"]
    pattern = re.compile(r"Payment\s+Due\s+Date", re.IGNORECASE)

    result = _find_period(lines, pattern, ("%d/%m/%Y",))

    assert result is None


@pytest.mark.parametrize(
    ("line", "pattern", "formats", "expected"),
    [
        pytest.param(
            "Statement of Axis Account No :000000000000000 for the period "
            "(From : 01-04-2026 To : 30-04-2026)",
            re.compile(r"From\s*:.*\bTo\s*:"),
            ("%d-%m-%Y",),
            (date(2026, 4, 1), date(2026, 4, 30)),
            id="axis-title",
        ),
        pytest.param(
            "Statement Period (01/03/2026 - 31/03/2026).",
            re.compile(r"Statement\s+Period", re.IGNORECASE),
            ("%d/%m/%Y",),
            (date(2026, 3, 1), date(2026, 3, 31)),
            id="bracketed-full-stop",
        ),
    ],
)
def test_find_period_strips_punctuation_touching_a_date(
    line: str, pattern: re.Pattern[str], formats: tuple[str, ...], expected: tuple[date, date]
) -> None:
    assert _find_period([line], pattern, formats) == expected


def test_parse_statement_suppresses_the_original_context() -> None:
    """A52: a crash becomes a bare ``ParserError``; the original is hidden from tracebacks."""

    class _Crashes:
        @classmethod
        def parse(cls, pdf_bytes: bytes, password: str | None) -> ParsedStatement:
            raise ValueError("SENTINEL-TEXT")

    with pytest.raises(ParserError, match="statement parser failed") as info:
        parse_statement(_Crashes, b"x", None)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True
    assert "SENTINEL-TEXT" not in str(info.value)


def test_parse_statement_passes_parser_errors_through() -> None:
    class _Fails:
        @classmethod
        def parse(cls, pdf_bytes: bytes, password: str | None) -> ParsedStatement:
            raise ParserError("specific")

    with pytest.raises(ParserError, match="^specific$"):
        parse_statement(_Fails, b"x", None)


# ---------------------------------------------------------------------------
# _extract_word_table: a positioned-word reader for layouts with no ruled table.
# ---------------------------------------------------------------------------

_Pages = list[list[tuple[float, float, str]]]
_HEADER_LABELS = ("Date", "Narration", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance")
_STOP = re.compile(r"^\s*STATEMENT\s*SUMMARY", re.IGNORECASE)
_HEADER_LINE: list[tuple[float, float, str]] = [
    (40, 700, "Date"),
    (100, 700, "Narration"),
    (300, 700, "Withdrawal Amt."),
    (380, 700, "Deposit Amt."),
    (460, 700, "Closing Balance"),
]


def _word_table(pdf: bytes) -> list[list[list[str]]]:
    return _extract_word_table(pdf, headers=[_HEADER_LABELS], stop_re=_STOP)


def test_extract_word_table_assigns_words_to_header_columns(
    positioned_pdf_bytes: Callable[[_Pages], bytes],
) -> None:
    pdf = positioned_pdf_bytes(
        [
            [
                *_HEADER_LINE,
                (40, 686, "01/04/26"),
                (100, 686, "UPI-SENTINEL GROCER"),
                # Right-aligned: the amount starts left of its label's x0 (300).
                (285, 686, "1,250.00"),
                (440, 686, "1,98,750.00"),
                (40, 672, "02/04/26"),
                (100, 672, "NEFT CR-SENTINEL"),
                (392, 672, "75,000.00"),
                (440, 672, "2,73,750.00"),
            ]
        ]
    )
    rows = _word_table(pdf)[0]
    assert rows[0] == list(_HEADER_LABELS)
    assert rows[1] == ["01/04/26", "UPI-SENTINEL GROCER", "1,250.00", "", "1,98,750.00"]
    assert rows[2] == ["02/04/26", "NEFT CR-SENTINEL", "", "75,000.00", "2,73,750.00"]


def test_extract_word_table_carries_columns_to_a_page_without_header(
    positioned_pdf_bytes: Callable[[_Pages], bytes],
) -> None:
    pdf = positioned_pdf_bytes(
        [
            [*_HEADER_LINE, (40, 686, "01/04/26"), (285, 686, "1.00"), (440, 686, "9.00")],
            [(100, 740, "MUMBAI"), (40, 726, "02/04/26"), (392, 726, "5.00"), (440, 726, "14.00")],
        ]
    )
    pages = _word_table(pdf)
    assert pages[1] == [
        ["", "MUMBAI", "", "", ""],
        ["02/04/26", "", "", "5.00", "14.00"],
    ]


def test_extract_word_table_stops_at_a_stop_line(
    positioned_pdf_bytes: Callable[[_Pages], bytes],
) -> None:
    pdf = positioned_pdf_bytes(
        [
            [
                *_HEADER_LINE,
                (40, 686, "01/04/26"),
                (440, 686, "9.00"),
                (40, 600, "STATEMENT SUMMARY"),
                (40, 586, "03/04/26"),
            ]
        ]
    )
    rows = _word_table(pdf)[0]
    assert [r[0] for r in rows] == ["Date", "01/04/26"]


def test_extract_word_table_matches_a_spaceless_header(
    positioned_pdf_bytes: Callable[[_Pages], bytes],
) -> None:
    pdf = positioned_pdf_bytes(
        [
            [
                (40, 700, "Date"),
                (100, 700, "Narration"),
                (300, 700, "WithdrawalAmt."),
                (380, 700, "DepositAmt."),
                (460, 700, "ClosingBalance*"),
                (440, 686, "9.00"),
            ]
        ]
    )
    rows = _word_table(pdf)[0]
    assert rows[0] == ["Date", "Narration", "WithdrawalAmt.", "DepositAmt.", "ClosingBalance*"]
    assert rows[1] == ["", "", "", "", "9.00"]


def test_extract_word_table_yields_nothing_before_the_first_header(
    positioned_pdf_bytes: Callable[[_Pages], bytes],
) -> None:
    pdf = positioned_pdf_bytes(
        [
            [
                (40, 710, "TEST USER"),
                # Looks like the header but is not all of it: not a header.
                (40, 700, "Date"),
                (100, 700, "Narration"),
                (300, 700, "Withdrawal Amt."),
                (380, 700, "Deposit Amt."),
                (460, 700, "Closing Bal"),
                (440, 686, "9.00"),
            ],
            [*_HEADER_LINE, (40, 686, "01/04/26")],
        ]
    )
    pages = _word_table(pdf)
    assert pages[0] == []
    assert pages[1][1][0] == "01/04/26"


def test_extract_word_table_reads_a_page_whose_stop_line_sits_above_its_header(
    positioned_pdf_bytes: Callable[[_Pages], bytes],
) -> None:
    pdf = positioned_pdf_bytes(
        [
            [*_HEADER_LINE, (40, 686, "01/04/26"), (440, 686, "9.00")],
            [
                (40, 740, "STATEMENT SUMMARY"),
                *_HEADER_LINE,
                (40, 686, "02/04/26"),
                (285, 686, "1.00"),
                (440, 686, "8.00"),
            ],
        ]
    )
    pages = _word_table(pdf)
    assert pages[1] == [list(_HEADER_LABELS), ["02/04/26", "", "1.00", "", "8.00"]]


def test_extract_word_table_stops_a_headerless_page_at_a_stop_line(
    positioned_pdf_bytes: Callable[[_Pages], bytes],
) -> None:
    pdf = positioned_pdf_bytes(
        [
            [*_HEADER_LINE, (40, 686, "01/04/26"), (440, 686, "9.00")],
            [(40, 740, "STATEMENT SUMMARY"), (40, 726, "02/04/26")],
        ]
    )
    assert _word_table(pdf)[1] == []


def test_extract_word_table_preserves_rows_above_second_header_on_same_page(
    positioned_pdf_bytes: Callable[[_Pages], bytes],
) -> None:
    header_2 = [(x, 650, text) for x, _, text in _HEADER_LINE]
    pdf = positioned_pdf_bytes(
        [
            [
                *_HEADER_LINE,
                (40, 686, "01/04/26"),
                (100, 686, "UPI-ROW-1"),
                (285, 686, "1,000.00"),
                (440, 686, "9,000.00"),
                *header_2,
                (40, 630, "02/04/26"),
                (100, 630, "UPI-ROW-2"),
                (285, 630, "2,000.00"),
                (440, 630, "7,000.00"),
            ]
        ]
    )
    rows = _word_table(pdf)[0]
    assert len(rows) == 4
    assert rows[0] == list(_HEADER_LABELS)
    assert rows[1] == ["01/04/26", "UPI-ROW-1", "1,000.00", "", "9,000.00"]
    assert rows[2] == list(_HEADER_LABELS)
    assert rows[3] == ["02/04/26", "UPI-ROW-2", "2,000.00", "", "7,000.00"]


def test_extract_word_table_corrupt_bytes_raises() -> None:
    with pytest.raises(ParserError, match="failed to extract words"):
        _word_table(b"not a pdf")
