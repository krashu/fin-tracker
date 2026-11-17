"""Axis Bank savings-account statement parser (PRD §F1, ADR-0010 rules 4, 5, 7, 11, 12).

Two layouts are read by one reader over ``_extract_word_table`` (positioned words, so ruled and
unruled PDFs both work):

* **current.** Title ``Statement of Axis Account No :… for the period (From : … To : …)``,
  header ``Tran Date | Chq No | Particulars | Debit | Credit | Balance | Init.(Br)``, an
  ``OPENING BALANCE`` row inside the table, then ``TRANSACTION TOTAL`` and ``CLOSING BALANCE``.
* **legacy.** The same columns with the header ending ``Init.``, comma-grouped opening and
  closing figures, a ``<token> TOTAL`` row and an ``End of Statement`` footer.

Dates are ``DD-MM-YYYY``. A narration wraps onto the lines below its date line (a top-aligned
reader), so a line with no date joins the open row. The column a row's amount sits in types it;
the sign never comes from the balance change. A deposit whose narration reads as a reversal is
typed ``refund`` (it imports as a positive ``spend`` that nets the debit).

The in-table ``OPENING BALANCE`` row seeds the row-1 running-balance check inside
:meth:`AxisBank.interpret_tables`; :meth:`AxisBank.parse` falls back to the summary's opening
when the PDF prints it above the header, outside the word table. A PDF whose table header is
never found raises :class:`ParserError`; a header with no rows is a valid 0-row statement.
Oldest-first order is assumed: a newest-first file fails the running-balance check loudly
rather than being guessed at.

Layout facts were read (nothing adapted, so no ``THIRD_PARTY_NOTICES.md`` entry) from
https://github.com/raptar231/indian-bank-statement-parser at commit
53a2486b1726d7882d208c6856463dc312bcd075 (Apache-2.0). That project infers debit or credit from
the balance change, which ADR-0010 rule 12 forbids, so none of its code is used.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.parsers.bank import BankRow, OpenRow, parse_bank_balance, to_transactions
from app.parsers.base import (
    ParsedStatement,
    ParserError,
    RawTransaction,
    StatementSummary,
    _decrypt,
    _extract_text,
    _extract_word_table,
    _find_period,
    _try_parse_date,
)

_DATE_FORMATS = ("%d-%m-%Y",)

_LABELS_BR = ("Tran Date", "Chq No", "Particulars", "Debit", "Credit", "Balance", "Init.(Br)")
_LABELS_INIT = ("Tran Date", "Chq No", "Particulars", "Debit", "Credit", "Balance", "Init.")
# Order matters: ``_match_header`` joins words, so ``Init.(Br)`` matches both ``Init.(Br)`` and
# ``Init.`` ``(Br)``; a bare ``Init.`` falls through to the second. The Init column is never
# dropped, or the branch code would merge into the Balance cell.
_HEADERS = (_LABELS_BR, _LABELS_INIT)
_STOP_RE = re.compile(r"End\s+of\s+Statement", re.IGNORECASE)

_OPENING_LABEL_RE = re.compile(r"OPENING\s+BALANCE", re.IGNORECASE)
_TOTAL_LABEL_RE = re.compile(r"(?:\S+\s+)?(?:TRANSACTION\s+)?TOTAL", re.IGNORECASE)
_CLOSING_LABEL_RE = re.compile(r"CLOSING\s+BALANCE", re.IGNORECASE)

_AMOUNT = r"(?P<amount>-?[\d,]+\.\d{2}(?:\s*(?:Cr|Dr))?)"
_OPENING_LINE_RE = re.compile(rf"^\s*OPENING\s+BALANCE\s+{_AMOUNT}\s*$", re.IGNORECASE)
_CLOSING_LINE_RE = re.compile(rf"^\s*CLOSING\s+BALANCE\s+{_AMOUNT}\s*$", re.IGNORECASE)
_PERIOD_RE = re.compile(r"From\s*:.*\bTo\s*:")
_TRAN_DATE_RE = re.compile(r"Tran\s*Date", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _Columns:
    date: int
    chq: int
    particulars: int
    debit: int
    credit: int
    balance: int


def _find(cells: Sequence[str], pattern: str) -> int | None:
    for i, cell in enumerate(cells):
        if re.search(pattern, cell.strip(), re.IGNORECASE):
            return i
    return None


def _header_columns(cells: Sequence[str], page: int) -> _Columns | None:
    date_i = _find(cells, r"^Tran\s*Date$")
    if date_i is None:
        return None
    chq = _find(cells, r"^Chq")
    particulars = _find(cells, r"^Particulars$")
    debit = _find(cells, r"^Debit$")
    credit = _find(cells, r"^Credit$")
    balance = _find(cells, r"^Balance$")
    if chq is None or particulars is None or debit is None or credit is None or balance is None:
        raise ParserError(f"cannot read the transaction table header on page {page}")
    return _Columns(date_i, chq, particulars, debit, credit, balance)


def _cell(cells: Sequence[str], i: int) -> str:
    return cells[i] if i < len(cells) else ""


def _label(cells: Sequence[str], cols: _Columns) -> str:
    parts = (_cell(cells, i).strip() for i in (cols.date, cols.chq, cols.particulars))
    return " ".join(" ".join(p for p in parts if p).split())


def _absorb(row: OpenRow, cells: Sequence[str], cols: _Columns) -> None:
    row.absorb(
        [_cell(cells, cols.particulars)],
        withdrawal=_cell(cells, cols.debit),
        deposit=_cell(cells, cols.credit),
        balance=_cell(cells, cols.balance),
    )


def _read_rows(tables: list[list[list[str]]]) -> tuple[int | None, list[BankRow]]:
    """The in-table opening balance (if any) and the rows in printed order."""
    cols: _Columns | None = None
    seen_header = False
    ended = False
    opening: int | None = None
    rows: list[BankRow] = []
    open_row: OpenRow | None = None

    def close() -> None:
        nonlocal open_row
        if open_row is not None:
            rows.append(open_row.close())
            open_row = None

    for index, page_rows in enumerate(tables):
        page = index + 1
        ordinal = 0
        for cells in page_rows:
            header = _header_columns(cells, page)
            if header is not None:
                close()
                cols, seen_header, ended = header, True, False
                continue
            if cols is None or ended:
                continue
            row_date = _try_parse_date(_cell(cells, cols.date), _DATE_FORMATS)
            if row_date is not None:
                close()
                ordinal += 1
                started = OpenRow(row_date, page, ordinal)
                _absorb(started, cells, cols)
                open_row = started
                continue
            label = _label(cells, cols)
            if _OPENING_LABEL_RE.fullmatch(label):
                if opening is None and not rows and open_row is None:
                    opening = parse_bank_balance(_cell(cells, cols.balance))
                    if opening is None:
                        raise ParserError(f"cannot read the opening balance on page {page}")
                continue
            if (
                _TOTAL_LABEL_RE.fullmatch(label)
                and (_cell(cells, cols.debit).strip() or _cell(cells, cols.credit).strip())
                and not _cell(cells, cols.balance).strip()
            ):
                close()
                ended = True
                continue
            if _CLOSING_LABEL_RE.fullmatch(label):
                close()
                ended = True
                continue
            if _cell(cells, cols.date).strip():
                close()
                continue
            if open_row is not None:
                _absorb(open_row, cells, cols)
    close()
    if not seen_header:
        raise ParserError("cannot find the transaction table")
    return opening, rows


class AxisBank:
    """Axis Bank savings-account statement parser."""

    @classmethod
    def parse(cls, pdf_bytes: bytes, password: str | None) -> ParsedStatement:
        decrypted = _decrypt(pdf_bytes, password)
        summary = cls.interpret_summary(_extract_text(decrypted))
        opening, rows = _read_rows(
            _extract_word_table(decrypted, headers=_HEADERS, stop_re=_STOP_RE)
        )
        if opening is None:  # an opening printed above the header lies outside the word table
            opening = summary.opening_balance_paise
        return ParsedStatement(rows=to_transactions(rows, opening_paise=opening), summary=summary)

    @classmethod
    def interpret_tables(cls, tables: list[list[list[str]]]) -> list[RawTransaction]:
        opening, rows = _read_rows(tables)
        return to_transactions(rows, opening_paise=opening)

    @classmethod
    def interpret_summary(cls, lines: Sequence[str]) -> StatementSummary:
        opening = closing = None
        for line in lines:
            if opening is None and (m := _OPENING_LINE_RE.match(line)):
                opening = parse_bank_balance(m["amount"])
            if closing is None and (m := _CLOSING_LINE_RE.match(line)):
                closing = parse_bank_balance(m["amount"])
        period = _find_period(lines, _PERIOD_RE, _DATE_FORMATS)
        return StatementSummary(
            opening_balance_paise=opening,
            closing_balance_paise=closing,
            period_start=period[0] if period else None,
            period_end=period[1] if period else None,
        )
