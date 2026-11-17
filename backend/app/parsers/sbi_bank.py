"""SBI savings-account statement parser (PRD §F1, ADR-0010 rules 4, 5, 7, 11, 12).

Three layouts are read, all producing :class:`~app.parsers.bank.BankRow` values that
:func:`~app.parsers.bank.to_transactions` types and checks against the printed running balance:

* **netbanking**, the onlinesbi "Account Statement". Header ``Txn Date | Value Date |
  Description | Ref No./Cheque No. | Debit | Credit | Balance``, dates ``1 Apr 2026``. The
  year wraps onto the next line of a row, so a row stays open until its year arrives. Read as
  a ruled table when there is one, else from positioned words.
* **estatement**, the e-account statement. Header ``Date | Transaction Reference | ... |
  Ref.No./Chq.No. | Credit | Debit | Balance`` (Credit is printed first, so columns are mapped
  by label, never by position), dates ``DD-MM-YY``, ``-`` or ``0`` for the side that does not
  apply.
* **yono**, the YONO "STATEMENT OF ACCOUNT". Only ``Balance`` is labelled in the text layer;
  Debit and Credit are the two columns to its left. Dates ``DD/MM/YYYY``; the narration carries
  a type prefix and a branch stamp that are dropped.

The column a row's amount sits in types it; the sign never comes from the balance change.

A combined e-account file (more than one account section) is refused rather than read: a
statement covers one account. The netbanking statement prints no closing balance, so its
closing is the last printed running balance, read from the statement text independently of the
table read (ADR-0010 rule 9). The opening balance is read from the text, so the first-row
running-balance check runs in :meth:`SbiBank.parse`.

The narration clean-up (:func:`_join_wrapped`, :func:`_clean_yono_narration`), the YONO column
rule and the e-account opening-balance fragment are adapted from
``bank_statement_parser/parsers/sbi.py`` in https://github.com/akhilnarang/bank-statement-parser
at commit 40fd618cebf6688d8316a5a7a5d116d781c97b1e, MIT licence (see ``THIRD_PARTY_NOTICES.md``).
The raptar231/indian-bank-statement-parser repository was read for layout facts only; nothing
is adapted from it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.parsers.bank import BankRow, OpenRow, parse_bank_balance, to_transactions
from app.parsers.base import (
    ParsedStatement,
    ParserError,
    RawTransaction,
    StatementSummary,
    _decrypt,
    _extract_tables,
    _extract_text,
    _extract_word_table,
    _find_period,
    _try_parse_date,
)

_NB_FORMATS = ("%d %b %Y",)
_ESTMT_FORMATS = ("%d-%m-%y",)
_YONO_FORMATS = ("%d/%m/%Y",)

_NB_LABELS = (
    "Txn Date",
    "Value Date",
    "Description",
    "Ref No./Cheque No.",
    "Debit",
    "Credit",
    "Balance",
)
_NB_WRAPPED_LABELS = (
    "Txn Date",
    "Value",
    "Description",
    "Ref No./Cheque",
    "Debit",
    "Credit",
    "Balance",
)

_NB_STOP_RE = re.compile(
    r"^\s*(?:Please\s+do\s+not\s+share|\*+\s*This\s+is\s+a\s+computer\s+generated)", re.IGNORECASE
)
_NB_HEADER_RE = re.compile(r"Txn\s*Date.*Description.*Debit.*Credit.*Balance", re.IGNORECASE)
_BALANCE = r"-?[\d,]+\.\d{2}(?:\s*(?:CR|DR)\b)?"
_NB_OPENING_RE = re.compile(
    rf"Balance\s+as\s+on\s+\d{{1,2}}\s+[A-Za-z]{{3}}\s+\d{{4}}\s*:\s*(?P<amount>{_BALANCE})",
    re.IGNORECASE,
)
_NB_PERIOD_RE = re.compile(
    r"Statement\s+from\s+(?P<start>\d{1,2}\s+[A-Za-z]{3}\s+\d{4})"
    r"\s+to\s+(?P<end>\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
    re.IGNORECASE,
)
# A transaction line: starts with a day and month, ends with an amount (or "-") and a balance.
_NB_ROW_LINE_RE = re.compile(
    rf"^\s*\d{{1,2}}\s+[A-Za-z]{{3}}\b.*\s(?:-|[\d,]+\.\d{{2}})\s+(?P<balance>{_BALANCE})\s*$",
    re.IGNORECASE,
)

_DAY_MONTH_RE = re.compile(r"\d{1,2}\s+[A-Za-z]{3}")
_YEAR_RE = re.compile(r"\d{4}")
_ENDS_WITH_YEAR_RE = re.compile(r"\d{4}$")

_SECTION_RE = re.compile(r"^\s*TRANSACTION\s+DETAILS\s*$")
_SAVINGS_RE = re.compile(r"SAVINGS?\s+ACCOUNT\b", re.IGNORECASE)

_ON_DATE_BALANCE_RE = re.compile(
    rf"\bon\s+(?P<date>\d{{2}}-\d{{2}}-\d{{2}})\s*:\s*(?P<amount>{_BALANCE})", re.IGNORECASE
)
_CLOSING_RE = re.compile(r"Closing", re.IGNORECASE)

_BROUGHT_FORWARD_RE = re.compile(r"Brought\s*Forward.*Closing\s*Balance\W*$", re.IGNORECASE)
_YONO_PERIOD_RE = re.compile(r"Statement\s+(?:From|Summary)\b", re.IGNORECASE)
_AMOUNT_TOKEN_RE = re.compile(_BALANCE, re.IGNORECASE)

_SEPARATORS = ("/", "-", "*")
_BRANCH_STAMP_RE = re.compile(r"\s*\d{10,}\s+AT\s+\d{4,5}\b.*$")
_TYPE_PREFIX_RE = re.compile(r"^(?:DEP TFR|WDL TFR|POS ATM PURCH)\s+")


@dataclass(frozen=True, slots=True)
class _Columns:
    """Where the cells we read sit in a table row, from the header row."""

    layout: Literal["netbanking", "estatement", "yono"]
    date: int
    narration: int
    debit: int
    credit: int
    balance: int
    date_formats: tuple[str, ...]


def _find_column(cells: Sequence[str], pattern: str) -> int | None:
    for i, cell in enumerate(cells):
        if re.search(pattern, cell, re.IGNORECASE):
            return i
    return None


def _cell(cells: Sequence[str], index: int) -> str:
    return cells[index] if index < len(cells) else ""


def _header_columns(cells: Sequence[str], page: int) -> _Columns | None:
    """The column map when ``cells`` is a transaction-table header row, else ``None``."""
    norm = [" ".join(c.split()) for c in cells]
    joined = " ".join(norm)
    layout: Literal["netbanking", "estatement", "yono"]
    if re.search(r"Transaction\s*Reference", joined, re.IGNORECASE) and all(
        any(re.fullmatch(label, c, re.IGNORECASE) for c in norm)
        for label in ("Credit", "Debit", "Balance")
    ):
        layout = "estatement"
        found = (
            _find_column(norm, r"^Date$"),
            _find_column(norm, r"Transaction\s*Reference"),
            _find_column(norm, r"^Debit$"),
            _find_column(norm, r"^Credit$"),
            _find_column(norm, r"^Balance$"),
        )
        formats = _ESTMT_FORMATS
    elif re.search(r"Txn\s*Date", joined, re.IGNORECASE) and re.search(
        r"Description", joined, re.IGNORECASE
    ):
        layout = "netbanking"
        found = (
            _find_column(norm, r"Txn\s*Date"),
            _find_column(norm, r"Description"),
            _find_column(norm, r"^Debit$"),
            _find_column(norm, r"^Credit$"),
            _find_column(norm, r"^Balance$"),
        )
        formats = _NB_FORMATS
    else:
        filled = [i for i, c in enumerate(norm) if c]
        if len(filled) == 1 and norm[filled[0]] == "Balance" and filled[0] >= 4:
            b = filled[0]
            return _Columns("yono", 0, 2, b - 2, b - 1, b, _YONO_FORMATS)
        return None
    if any(i is None for i in found):
        raise ParserError(f"cannot read the transaction table header on page {page}")
    d, n, debit, credit, b = found
    assert d is not None and n is not None and debit is not None  # narrowed above
    assert credit is not None and b is not None
    return _Columns(layout, d, n, debit, credit, b, formats)


def _has_header(tables: list[list[list[str]]]) -> bool:
    return any(
        _header_columns(cells, page_index + 1) is not None
        for page_index, page_rows in enumerate(tables)
        for cells in page_rows
    )


def _join_wrapped(parts: Sequence[str]) -> str:
    """One narration from its wrapped lines.

    Adapted from akhilnarang's ``_yono_narration`` (MIT): a line that ends or starts at a
    ``/``, ``-`` or ``*`` break is glued to its neighbour with no space; any other break is a
    space.
    """
    text = ""
    for raw in parts:
        part = " ".join(raw.split())
        if not part:
            continue
        if not text:
            text = part
        elif text.endswith(_SEPARATORS) or part.startswith(_SEPARATORS):
            text += part
        else:
            text += " " + part
    return text


def _clean_yono_narration(text: str) -> str:
    """Drop the YONO branch stamp and transaction-type prefix (adapted from akhilnarang, MIT)."""
    return _TYPE_PREFIX_RE.sub("", _BRANCH_STAMP_RE.sub("", text))


def _join_yono(parts: Sequence[str]) -> str:
    return _clean_yono_narration(_join_wrapped(parts))


def _amount_cell(cells: Sequence[str], index: int) -> str:
    """An amount cell, with SBI's ``-`` (nothing in this column) read as blank."""
    value = _cell(cells, index).strip()
    return "" if value == "-" else value


@dataclass(slots=True)
class _SbiRow:
    """A transaction being assembled from its date line and continuation lines.

    Keeps the date as printed until close, because a netbanking date may wrap its year onto
    the next line; the shared :class:`OpenRow` holds everything else.
    """

    date_text: str
    cols: _Columns
    draft: OpenRow

    @property
    def awaiting_year(self) -> bool:
        """A netbanking row whose date still lacks the year printed on its next line."""
        return self.cols.layout == "netbanking" and not _ENDS_WITH_YEAR_RE.search(self.date_text)

    def absorb(self, cells: Sequence[str], cols: _Columns) -> None:
        self.draft.absorb(
            _cell(cells, cols.narration).split("\n"),
            withdrawal=_amount_cell(cells, cols.debit),
            deposit=_amount_cell(cells, cols.credit),
            balance=_amount_cell(cells, cols.balance),
        )

    def close(self) -> BankRow:
        self.draft.date = _try_parse_date(self.date_text, self.cols.date_formats)
        return self.draft.close(_join_yono if self.cols.layout == "yono" else _join_wrapped)


def _starts_row(date_text: str, cols: _Columns) -> bool:
    if _try_parse_date(date_text, cols.date_formats) is not None:
        return True
    return cols.layout == "netbanking" and _DAY_MONTH_RE.fullmatch(date_text) is not None


def _check_account_sections(lines: Sequence[str]) -> None:
    """Refuse a file holding more than one account section, or no savings section.

    A section is ``TRANSACTION DETAILS`` followed by its kind line and its account line. The
    same heading repeated on every page is one section.
    """
    headings: set[tuple[str, str]] = set()
    for i, line in enumerate(lines):
        if not _SECTION_RE.match(line):
            continue
        following = [ln.strip() for ln in lines[i + 1 :] if ln.strip()][:2]
        following += [""] * (2 - len(following))
        headings.add((following[0], following[1]))
    if not headings:
        return
    if len(headings) > 1:
        raise ParserError("statement holds more than one account section")
    ((kind, _number),) = headings
    if _SAVINGS_RE.match(kind) is None:
        raise ParserError("statement holds no savings account section")


def _read_estatement_summary(lines: Sequence[str]) -> StatementSummary:
    """Opening and closing balance and period from the ``... on DD-MM-YY: X`` lines.

    The opening label can come out garbled, so it is matched on its ``on DD-MM-YY:`` fragment
    (adapted from akhilnarang, MIT); the period is those two dates.
    """
    opening: int | None = None
    closing: int | None = None
    start = end = None
    for line in lines:
        match = _ON_DATE_BALANCE_RE.search(line)
        if match is None:
            continue
        when = _try_parse_date(match.group("date"), _ESTMT_FORMATS)
        amount = parse_bank_balance(match.group("amount"))
        if _CLOSING_RE.search(line):
            if closing is None:
                closing, end = amount, when
        elif opening is None:
            opening, start = amount, when
    return StatementSummary(opening, closing, start, end)


def _read_yono_summary(lines: Sequence[str]) -> StatementSummary:
    opening: int | None = None
    closing: int | None = None
    for i, line in enumerate(lines[:-1]):
        if _BROUGHT_FORWARD_RE.search(line):
            tokens = _AMOUNT_TOKEN_RE.findall(lines[i + 1])
            if len(tokens) >= 2:
                opening = parse_bank_balance(tokens[0])
                closing = parse_bank_balance(tokens[-1])
            break
    period = _find_period(lines, _YONO_PERIOD_RE, ("%d-%m-%Y",))
    if opening is None and closing is None and period is None:
        return StatementSummary()
    return StatementSummary(
        opening, closing, period[0] if period else None, period[1] if period else None
    )


def _read_netbanking_summary(lines: Sequence[str]) -> StatementSummary:
    """The opening from ``Balance as on``; the closing is the balance on the last printed
    transaction line (netbanking prints none), or the opening when no row is printed."""
    opening: int | None = None
    for line in lines:
        match = _NB_OPENING_RE.search(line)
        if match is not None:
            opening = parse_bank_balance(match.group("amount"))
            break
    closing = opening
    for line in lines:
        match = _NB_ROW_LINE_RE.match(line)
        if match is not None:
            closing = parse_bank_balance(match.group("balance"))
    start = end = None
    for line in lines:
        match = _NB_PERIOD_RE.search(line)
        if match is not None:
            start = _try_parse_date(match.group("start"), _NB_FORMATS)
            end = _try_parse_date(match.group("end"), _NB_FORMATS)
            break
    return StatementSummary(opening, closing, start, end)


class SbiBank:
    """SBI savings-account statement parser."""

    @classmethod
    def _read_rows(cls, tables: list[list[list[str]]]) -> list[BankRow]:
        """Walk ``pages → rows → cells``; the columns and the open row carry across pages.

        A header found with no rows is a valid empty batch; no header at all is an error
        (ADR-0010 rule 5).
        """
        rows: list[BankRow] = []
        cols: _Columns | None = None
        open_row: _SbiRow | None = None
        seen_header = False
        for page_index, page_rows in enumerate(tables):
            page = page_index + 1
            ordinal = 0
            for cells in page_rows:
                header = _header_columns(cells, page)
                if header is not None:
                    seen_header = True
                    # A netbanking row whose year is still to come survives a repeated header.
                    if open_row is not None and not open_row.awaiting_year:
                        rows.append(open_row.close())
                        open_row = None
                    cols = header
                    continue
                if cols is None:
                    continue
                date_text = " ".join(_cell(cells, cols.date).split())
                if not date_text:
                    if open_row is not None:
                        open_row.absorb(cells, cols)
                elif _starts_row(date_text, cols):
                    if open_row is not None:
                        rows.append(open_row.close())
                    ordinal += 1
                    open_row = _SbiRow(date_text, cols, OpenRow(None, page, ordinal))
                    open_row.absorb(cells, cols)
                elif (
                    open_row is not None
                    and open_row.awaiting_year
                    and _YEAR_RE.fullmatch(date_text)
                ):
                    open_row.date_text += " " + date_text
                    open_row.absorb(cells, cols)
                elif open_row is not None:
                    # Text in the date column that is not a date (a balance or summary row).
                    rows.append(open_row.close())
                    open_row = None
        if open_row is not None:
            rows.append(open_row.close())
        if not seen_header:
            raise ParserError("cannot find the transaction table")
        return rows

    @classmethod
    def interpret_tables(cls, tables: list[list[list[str]]]) -> list[RawTransaction]:
        """Rows from extracted tables. Without the summary there is no opening balance, so
        the running-balance check starts at the second row; :meth:`parse` checks the first."""
        return to_transactions(cls._read_rows(tables), opening_paise=None)

    @classmethod
    def interpret_summary(cls, lines: Sequence[str]) -> StatementSummary:
        """Opening and closing balance and the period, each ``None`` if not readable."""
        for read in (_read_estatement_summary, _read_yono_summary, _read_netbanking_summary):
            summary = read(lines)
            if summary != StatementSummary():
                return summary
        return StatementSummary()

    @classmethod
    def parse(cls, pdf_bytes: bytes, password: str | None) -> ParsedStatement:
        decrypted = _decrypt(pdf_bytes, password)
        lines = _extract_text(decrypted)
        _check_account_sections(lines)
        summary = cls.interpret_summary(lines)
        tables = _extract_tables(decrypted)
        if not _has_header(tables) and any(_NB_HEADER_RE.search(line) for line in lines):
            tables = _extract_word_table(
                decrypted, headers=(_NB_LABELS, _NB_WRAPPED_LABELS), stop_re=_NB_STOP_RE
            )
        rows = to_transactions(cls._read_rows(tables), opening_paise=summary.opening_balance_paise)
        return ParsedStatement(rows=rows, summary=summary)
