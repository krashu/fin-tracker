"""ICICI Bank savings-account statement parser (PRD §F1, ADR-0010 rules 4, 5, 7, 11, 12).

Two layouts of "Statement of Transactions in Savings Account" are read from positioned words
(:func:`~app.parsers.base._extract_word_table`), each producing
:class:`~app.parsers.bank.BankRow` values that :func:`~app.parsers.bank.to_transactions`
types and checks against the printed running balance:

* **Current e-statement.** Header ``DATE MODE PARTICULARS DEPOSITS WITHDRAWALS BALANCE``,
  dates ``DD-MM-YYYY``, Indian digit grouping. A row's particulars are centred on its date
  line: as many lines below it as above it, and the date line itself may carry particulars.
  The reader takes the narration in the order MODE, lines above, the date line's particulars,
  lines below.
* **Pre-2020.** Header ``DATE MODE DEPOSITS WITHDRAWALS BALANCE``: no PARTICULARS label, the
  narration sits in the MODE column. A row starts at its date line and continues downward,
  across a page break if need be, and its amounts may sit on a later line of the row.

A current-layout row is read within its page: its centred narration cannot be split across
pages without the page geometry.

Both print Deposits before Withdrawals and an opening-balance row ``B/F`` inside the table.
No closing balance is printed, so the closing balance is the last printed running balance,
read from the statement text independently of the table read (ADR-0010 rule 7).

* The column an amount sits in decides its sign; the balance change never does.
* The ``B/F`` row is the opening balance: :meth:`IciciBank.interpret_tables` checks row 1
  against it (ADR-0010 rule 11). A repeated ``B/F`` (after a page break) is skipped.
* MODE text leads ``merchant_raw``.
* A PDF in which no transaction-table header is found is refused (``cannot find the
  transaction table``); a header with no rows under it is a valid empty statement
  (ADR-0010 rule 5). The netbanking "Detailed Statement" export is refused this way.
* ``row`` is the transaction's ordinal on its page; ``B/F`` is not counted.
* ``_extract_word_table`` can put the tail of a left-aligned particulars line into the DEPOSITS
  cell; :func:`_split_amount` returns such words to the narration.

The centred-narration rule (a row takes as many particulars lines below its date line as above
it) and the narration order are adapted from ``bank_statement_parser/parsers/icici.py``
(``IciciBankStatementParser._parse_icici_page``, ``_TABLE_END_MARKERS``) in
https://github.com/akhilnarang/bank-statement-parser at commit
40fd618cebf6688d8316a5a7a5d116d781c97b1e, MIT licence (see ``THIRD_PARTY_NOTICES.md``). Ours
counts lines instead of measuring their vertical distance, because ``_extract_word_table``
yields rows without positions. raptar231/indian-bank-statement-parser (Apache-2.0) and
juneja-varun/expense-analyser (AGPL-3.0) were read for layout facts only; nothing is adapted
from them.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from app.parsers.bank import BankRow, OpenRow, parse_bank_balance, to_transactions
from app.parsers.base import (
    ParsedStatement,
    ParserError,
    RawTransaction,
    StatementSummary,
    _decrypt,
    _extract_text,
    _extract_word_table,
    _normalise_label,
    _try_parse_date,
)

_DATE_FORMATS = ("%d-%m-%Y",)
_PERIOD_DATE_FORMATS = ("%B %d %Y", "%b %d %Y")
_LABELS = ("DATE", "MODE", "PARTICULARS", "DEPOSITS", "WITHDRAWALS", "BALANCE")
_LEGACY_LABELS = ("DATE", "MODE", "DEPOSITS", "WITHDRAWALS", "BALANCE")
# Where a page's rows end. "Total:" needs its colon so a "TOTAL ENERGIES" name line cannot end a
# page. "Page N of M" is not a stop: where ICICI prints it is unverified, and a page-top match on a
# page with no header of its own would hide that page.
_STOP_RE = re.compile(
    r"^\s*(?:(?:SUB|GRAND)\s+)?TOTAL\s*:|^\s*(?:ACCOUNT\s+RELATED\s+OTHER\s+INFORMATION"
    r"|LEGENDS\s+FOR\s+TRANSACTIONS|THIS\s+IS\s+A\s+(?:SYSTEM|COMPUTER)\s+GENERATED"
    r"|CARD\s+BLOCKING\s+PROCEDURE|ACCOUNT\s+BLOCKING\s+PROCEDURE|SINCERELY"
    r"|STATEMENT\s+OF\s+LINKED\s+FIXED\s+DEPOSITS)",
    re.IGNORECASE,
)
_TABLE_HEAD_RE = re.compile(r"^\s*DATE\s+MODE\b", re.IGNORECASE)
_FD_RE = re.compile(r"^\s*STATEMENT\s+OF\s+LINKED\s+FIXED\s+DEPOSITS", re.IGNORECASE)
_TOTAL_RE = re.compile(r"^\s*(?:(?:SUB|GRAND)\s+)?TOTAL\s*:", re.IGNORECASE)
_BALANCE = r"-?[\d,]+\.\d{2}(?:\s*(?:Cr|Dr)\b)?"
_BF_LINE_RE = re.compile(rf"^\s*\d{{2}}-\d{{2}}-\d{{4}}\s+B/F\s+({_BALANCE})", re.IGNORECASE)
_ROW_END_RE = re.compile(rf"[\d,]+\.\d{{2}}\s+({_BALANCE})\s*$", re.IGNORECASE)
_PERIOD_RE = re.compile(
    r"for\s+the\s+period\s+([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\s*-\s*([A-Za-z]+)\s+(\d{1,2}),"
    r"\s*(\d{4})",
    re.IGNORECASE,
)
_AMOUNT_WORD_RE = re.compile(r"^\d[\d,]*\.\d{2}$")
_MARKER_RE = re.compile(r"^(?:Cr|Dr)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _Columns:
    """Where the cells we read sit in a table row; the date column is always index 0."""

    centred: bool
    mode: int
    particulars: int | None
    deposit: int
    withdrawal: int
    balance: int


def _header_columns(cells: Sequence[str]) -> _Columns | None:
    """The column map when ``cells`` is a transaction-table header row, else ``None``."""
    normalised = tuple(_normalise_label(c) for c in cells)
    if normalised == tuple(_normalise_label(x) for x in _LABELS):
        return _Columns(True, 1, 2, 3, 4, 5)
    if normalised == tuple(_normalise_label(x) for x in _LEGACY_LABELS):
        return _Columns(False, 1, None, 2, 3, 4)
    return None


def _cell(cells: Sequence[str], i: int | None) -> str:
    return cells[i] if i is not None and i < len(cells) else ""


def _split_amount(cell: str, *, marker: bool) -> tuple[str, list[str]]:
    """An amount cell as ``(amount, spill)``: narration words the column split put into an
    amount column go back to the narration, in column order."""
    words = cell.split()
    if (
        marker
        and len(words) >= 2
        and _MARKER_RE.match(words[-1])
        and _AMOUNT_WORD_RE.match(words[-2])
    ):
        return " ".join(words[-2:]), words[:-2]
    if words and _AMOUNT_WORD_RE.match(words[-1]):
        return words[-1], words[:-1]
    return "", words


@dataclass(frozen=True, slots=True)
class _Line:
    date: date | None
    lead: list[str]
    text: list[str]
    words: list[str]
    deposit: str
    withdrawal: str
    balance: str


def _read_line(cells: Sequence[str], cols: _Columns) -> _Line:
    first = _cell(cells, 0).split()
    parsed = _try_parse_date(first[0], _DATE_FORMATS) if first else None
    lead = [*first[1:], *_cell(cells, cols.mode).split()]
    particulars = _cell(cells, cols.particulars).split() if cols.centred else []
    deposit, deposit_spill = _split_amount(_cell(cells, cols.deposit), marker=False)
    withdrawal, withdrawal_spill = _split_amount(_cell(cells, cols.withdrawal), marker=False)
    balance, balance_spill = _split_amount(_cell(cells, cols.balance), marker=True)
    return _Line(
        date=parsed,
        lead=lead,
        text=[*particulars, *deposit_spill, *withdrawal_spill, *balance_spill],
        words=[w for c in cells for w in c.split()],
        deposit=deposit,
        withdrawal=withdrawal,
        balance=balance,
    )


def _is_bf(line: _Line) -> bool:
    return line.date is not None and "B/F" in [*line.lead, *line.text]


def _read_opening(line: _Line, page: int, opening: int | None) -> int | None:
    value = parse_bank_balance(line.balance)
    if value is None:
        raise ParserError(f"cannot read the opening balance on page {page}")
    return value if opening is None else opening


def _read_centred(
    lines: list[_Line], page: int, opening: int | None
) -> tuple[int | None, list[BankRow]]:
    rows: list[BankRow] = []
    pending: list[str] = []
    owed = 0
    current: OpenRow | None = None
    ordinal = 0
    for line in lines:
        if line.date is None:
            text = " ".join(line.words)
            if text:
                pending.append(text)
            continue
        take = min(owed, len(pending))
        if current is not None:
            current.absorb(pending[:take])
            rows.append(current.close())
            current = None
        above = pending[take:]
        pending = []
        if _is_bf(line):
            owed = 0
            opening = _read_opening(line, page, opening)
            continue
        owed = len(above)
        ordinal += 1
        current = OpenRow(line.date, page, ordinal)
        current.absorb(
            [*line.lead, *above, *line.text],
            deposit=line.deposit,
            withdrawal=line.withdrawal,
            balance=line.balance,
        )
    if current is not None:
        current.absorb(pending[:owed])
        rows.append(current.close())
    return opening, rows


def _read_top_aligned(
    lines: list[_Line], page: int, opening: int | None, current: OpenRow | None
) -> tuple[int | None, list[BankRow], OpenRow | None]:
    """Read one pre-2020 segment. ``current`` is the row still open from the previous page; the
    row left open at the end of the segment is returned, not closed, so it can carry on."""
    rows: list[BankRow] = []
    ordinal = 0
    for line in lines:
        if line.date is not None:
            if current is not None:
                rows.append(current.close())
                current = None
            if _is_bf(line):
                opening = _read_opening(line, page, opening)
                continue
            ordinal += 1
            current = OpenRow(line.date, page, ordinal)
            current.absorb(
                [*line.lead, *line.text],
                deposit=line.deposit,
                withdrawal=line.withdrawal,
                balance=line.balance,
            )
        elif current is not None:
            current.absorb(
                [*line.lead, *line.text],
                deposit=line.deposit,
                withdrawal=line.withdrawal,
                balance=line.balance,
            )
    return opening, rows, current


class IciciBank:
    """ICICI Bank savings-account statement parser."""

    @classmethod
    def _read_rows(cls, tables: list[list[list[str]]]) -> tuple[int | None, list[BankRow]]:
        """The ``B/F`` opening balance (``None`` if no row printed one) and the rows."""
        opening: int | None = None
        rows: list[BankRow] = []
        cols: _Columns | None = None
        carry: OpenRow | None = None
        for page_index, page_rows in enumerate(tables):
            page = page_index + 1
            segments: list[tuple[_Columns, list[_Line]]] = []
            for cells in page_rows:
                header = _header_columns(cells)
                if header is not None:
                    cols = header
                    segments.append((cols, []))
                    continue
                if cols is None:
                    continue
                if not segments:
                    segments.append((cols, []))
                segments[-1][1].append(_read_line(cells, cols))
            for seg_cols, lines in segments:
                if seg_cols.centred:
                    if carry is not None:
                        rows.append(carry.close())
                        carry = None
                    opening, read_rows = _read_centred(lines, page, opening)
                else:
                    opening, read_rows, carry = _read_top_aligned(lines, page, opening, carry)
                rows.extend(read_rows)
        if carry is not None:
            rows.append(carry.close())
        if cols is None:
            raise ParserError("cannot find the transaction table")
        return opening, rows

    @classmethod
    def interpret_tables(cls, tables: list[list[list[str]]]) -> list[RawTransaction]:
        """Rows from extracted tables; row 1 is checked against the ``B/F`` row."""
        opening, rows = cls._read_rows(tables)
        return to_transactions(rows, opening_paise=opening)

    @classmethod
    def interpret_summary(cls, lines: Sequence[str]) -> StatementSummary:
        """Opening and closing balance and the period, each ``None`` if not readable.

        The opening balance is the ``B/F`` row; the closing balance is the last printed
        running balance (ICICI prints no closing figure). Nothing counts before the table
        header, and the Linked Fixed Deposits section after the table is ignored.
        """
        start: date | None = None
        end: date | None = None
        m = _PERIOD_RE.search(" ".join(lines))
        if m is not None:
            start = _try_parse_date(f"{m[1]} {m[2]} {m[3]}", _PERIOD_DATE_FORMATS)
            end = _try_parse_date(f"{m[4]} {m[5]} {m[6]}", _PERIOD_DATE_FORMATS)
            if start is None or end is None:
                start = end = None
        opening: int | None = None
        closing: int | None = None
        in_table = False
        for line in lines:
            if not in_table:
                in_table = _TABLE_HEAD_RE.match(line) is not None
                continue
            if _FD_RE.match(line):
                break
            if _TOTAL_RE.match(line):
                continue
            bf = _BF_LINE_RE.match(line)
            if bf is not None:
                value = parse_bank_balance(bf.group(1))
                if opening is None:
                    opening = value
                closing = value
                continue
            tail = _ROW_END_RE.search(line)
            if tail is not None:
                closing = parse_bank_balance(tail.group(1))
        return StatementSummary(opening, closing, start, end)

    @classmethod
    def parse(cls, pdf_bytes: bytes, password: str | None) -> ParsedStatement:
        decrypted = _decrypt(pdf_bytes, password)
        summary = cls.interpret_summary(_extract_text(decrypted))
        tables = _extract_word_table(decrypted, headers=(_LABELS, _LEGACY_LABELS), stop_re=_STOP_RE)
        return ParsedStatement(rows=cls.interpret_tables(tables), summary=summary)
