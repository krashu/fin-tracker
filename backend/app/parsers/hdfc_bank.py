"""HDFC Bank savings-account statement parser (PRD §F1, ADR-0010 rules 4, 7, 11, 12).

Three layouts are read, one reader each, all producing :class:`~app.parsers.bank.BankRow`
values that :func:`~app.parsers.bank.to_transactions` types and checks against the printed
running balance:

* **A, the 2024+ e-statement.** A ruled table (``_extract_tables``) with the header
  ``Txn Date | Narration | Withdrawals | Deposits | Closing Balance``, dates ``DD/MM/YYYY``.
  The table packs one row per page: every cell holds that page's values joined by newlines,
  and each narration ends ``Value Dt DD/MM/YYYY [Ref X]``. The side that does not apply
  prints ``0.00``.
* **B, netbanking and the older e-statement.** No ruled table, so the rows come from
  ``_extract_word_table``: ``Date | Narration | Chq./Ref.No. | Value Dt | Withdrawal Amt. |
  Deposit Amt. | Closing Balance``, dates ``DD/MM/YY``, an empty cell for the side that does
  not apply. A narration wraps over several lines and its amounts may sit on any of them.
* **B', pre-2020.** The same word-table reader with ``Value Date | Withdrawal Amount |
  Deposit Amount | Closing Balance*`` headers, ``DD/MM/YYYY`` dates and ``0.00`` for the side
  that does not apply. Its summary prints each label and value on separate lines.

Which of A or B/B' a PDF is comes from its text: only B and B' print ``Withdrawal Amt`` /
``Withdrawal Amount``.

The column a row's amount sits in types it; the sign never comes from the balance change.
A PDF in which no transaction-table header is found is refused (``cannot find the
transaction table``); a header with no rows under it is a valid empty statement (ADR-0010
rule 5).

A narration that reads as a reversal is typed ``refund`` (so it imports as a positive
``spend`` that nets the debit); every other deposit stays ``deposit``.

The Layout A narration split (:func:`_split_narrations` and its ``Value Dt`` marker) is
adapted from ``bank_statement_parser/parsers/hdfc.py`` (``_group_narrations``,
``_clean_narration``) in https://github.com/akhilnarang/bank-statement-parser at commit
40fd618cebf6688d8316a5a7a5d116d781c97b1e, MIT licence (see ``THIRD_PARTY_NOTICES.md``).
Everything else is written from the layouts' facts.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.parsers.bank import (
    BankRow,
    OpenRow,
    parse_bank_amount,
    parse_bank_balance,
    to_transactions,
)
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

_A_DATE_FORMATS = ("%d/%m/%Y",)
_B_DATE_FORMATS = ("%d/%m/%y", "%d/%m/%Y")

_B_LABELS = (
    "Date",
    "Narration",
    "Chq./Ref.No.",
    "Value Dt",
    "Withdrawal Amt.",
    "Deposit Amt.",
    "Closing Balance",
)
_B_PRIME_LABELS = (
    "Date",
    "Narration",
    "Chq. / Ref No.",
    "Value Date",
    "Withdrawal Amount",
    "Deposit Amount",
    "Closing Balance*",
)

# Where a word-table page's rows end: a footer, the summary block, or the end marker.
_STOP_RE = re.compile(
    r"^\s*(?:Page\s*No|STATEMENT\s*SUMMARY|HDFC\s*BANK\s*LIMITED|\*+\s*Closing\s*Balance"
    r"|\*\*\s*END\s*OF\s*STATEMENT|Opening\s*Balance|Closing\s*Balance\s*$|Cr\s*Count"
    r"|Dr\s*Count)",
    re.IGNORECASE,
)
# Only B and B' print "Withdrawal Amt" / "Withdrawal Amount"; A prints "Withdrawals".
_WORD_LAYOUT_RE = re.compile(r"Withdrawal\s*(?:Amt|Amount)", re.IGNORECASE)

# Layout A: each narration ends "Value Dt DD/MM/YYYY [Ref X]".
_VALUE_DT_RE = re.compile(r"Value\s+Dt\s+\d{2}/\d{2}/\d{4}(?:\s+Ref\s+\S+)?")

_AMOUNT_TOKEN_RE = re.compile(r"-?[\d,]+\.\d{2}(?:\s*(?:Cr|Dr)\b)?", re.IGNORECASE)
_SUMMARY_HEAD_RE = re.compile(r"Opening\s*Balance\s*(?:Debit\s*Amount|Dr\s*Count)", re.IGNORECASE)
_LONE_OPENING_RE = re.compile(r"^\s*Opening\s*Balance\s*:?\s*$", re.IGNORECASE)
_LONE_CLOSING_RE = re.compile(r"^\s*Closing\s*Balance\s*\*?\s*:?\s*$", re.IGNORECASE)
_PERIOD_RE = re.compile(r"\d{2}/\d{2}/\d{2,4}\s+To\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _Columns:
    """Where the five cells we read sit in a table row, from the header row."""

    layout: Literal["A", "B"]
    date: int
    narration: int
    withdrawal: int
    deposit: int
    balance: int


def _find_column(cells: Sequence[str], pattern: str) -> int | None:
    for i, cell in enumerate(cells):
        if re.search(pattern, cell, re.IGNORECASE):
            return i
    return None


def _header_columns(cells: Sequence[str], page: int) -> _Columns | None:
    """The column map when ``cells`` is a transaction-table header row, else ``None``."""
    joined = " ".join(cells)
    if re.search(r"Txn\s*Date", joined, re.IGNORECASE):
        layout: Literal["A", "B"] = "A"
        date_pattern = r"Txn\s*Date"
    elif re.search(r"Value\s*D(?:t|ate)", joined, re.IGNORECASE) and re.search(
        r"Withdrawal", joined, re.IGNORECASE
    ):
        layout = "B"
        date_pattern = r"^\s*Date\s*$"
    else:
        return None
    found = (
        _find_column(cells, date_pattern),
        _find_column(cells, r"Narration"),
        _find_column(cells, r"Withdrawal"),
        _find_column(cells, r"Deposit"),
        _find_column(cells, r"Closing\s*Balance"),
    )
    if any(i is None for i in found):
        raise ParserError(f"cannot read the transaction table header on page {page}")
    d, n, w, dep, b = found
    assert d is not None and n is not None and w is not None  # narrowed above
    assert dep is not None and b is not None
    return _Columns(layout, d, n, w, dep, b)


def _cell(cells: Sequence[str], index: int) -> str:
    return cells[index] if index < len(cells) else ""


def _split_narrations(cell: str) -> list[str]:
    """One narration per transaction from a Layout A narration cell.

    Adapted from akhilnarang's ``_group_narrations`` / ``_clean_narration`` (MIT): join the
    lines with a space, then split on each ``Value Dt DD/MM/YYYY [Ref X]`` marker. The text
    before a marker is that transaction's narration; the marker itself is dropped.
    """
    parts = _VALUE_DT_RE.split(" ".join(cell.split("\n")))
    narrations = [" ".join(part.split()) for part in parts[:-1]]
    trailing = " ".join(parts[-1].split())
    if trailing:
        narrations.append(trailing)
    return narrations


def _read_layout_a(
    cells: Sequence[str], cols: _Columns, page: int, first_row: int
) -> list[BankRow]:
    """The transactions packed into one Layout A table row, or ``[]`` for a non-date row
    (the summary table that follows the transactions)."""
    dates = [d.strip() for d in _cell(cells, cols.date).split("\n") if d.strip()]
    if not dates or _try_parse_date(dates[0], _A_DATE_FORMATS) is None:
        return []
    withdrawals = [c.strip() for c in _cell(cells, cols.withdrawal).split("\n") if c.strip()]
    deposits = [c.strip() for c in _cell(cells, cols.deposit).split("\n") if c.strip()]
    balances = [c.strip() for c in _cell(cells, cols.balance).split("\n") if c.strip()]
    narrations = _split_narrations(_cell(cells, cols.narration))
    if not (len(dates) == len(withdrawals) == len(deposits) == len(balances) == len(narrations)):
        raise ParserError(f"cannot read the transaction table on page {page}")
    rows: list[BankRow] = []
    for i, text in enumerate(dates):
        txn_date = _try_parse_date(text, _A_DATE_FORMATS)
        balance = parse_bank_balance(balances[i])
        if txn_date is None or balance is None:
            raise ParserError(f"cannot read row on page {page}, row {first_row + i}")
        rows.append(
            BankRow(
                date=txn_date,
                narration=narrations[i],
                withdrawal_paise=parse_bank_amount(withdrawals[i]),
                deposit_paise=parse_bank_amount(deposits[i]),
                balance_paise=balance,
                page=page,
                row=first_row + i,
            )
        )
    return rows


def _absorb(row: OpenRow, cells: Sequence[str], cols: _Columns) -> None:
    row.absorb(
        [_cell(cells, cols.narration)],
        withdrawal=_cell(cells, cols.withdrawal),
        deposit=_cell(cells, cols.deposit),
        balance=_cell(cells, cols.balance),
    )


class HdfcBank:
    """HDFC Bank savings-account statement parser."""

    @classmethod
    def _read_rows(cls, tables: list[list[list[str]]]) -> list[BankRow]:
        """Walk ``pages → rows → cells`` through the reader the header row selects.

        The reader and the open Layout B transaction carry across pages: a continuation line
        at the top of a page belongs to the transaction that began on the previous one.
        """
        rows: list[BankRow] = []
        cols: _Columns | None = None
        open_row: OpenRow | None = None
        for page_index, page_rows in enumerate(tables):
            page = page_index + 1
            ordinal = 0
            for cells in page_rows:
                header = _header_columns(cells, page)
                if header is not None:
                    if open_row is not None:
                        rows.append(open_row.close())
                        open_row = None
                    cols = header
                    continue
                if cols is None:
                    continue
                if cols.layout == "A":
                    packed = _read_layout_a(cells, cols, page, ordinal + 1)
                    ordinal += len(packed)
                    rows.extend(packed)
                    continue
                date_text = _cell(cells, cols.date).strip()
                parsed = _try_parse_date(date_text, _B_DATE_FORMATS)
                if parsed is not None:
                    if open_row is not None:
                        rows.append(open_row.close())
                    ordinal += 1
                    open_row = OpenRow(parsed, page, ordinal)
                    _absorb(open_row, cells, cols)
                elif date_text:
                    # Text in the date column that is not a date (an address or name line).
                    if open_row is not None:
                        rows.append(open_row.close())
                        open_row = None
                elif open_row is not None:
                    _absorb(open_row, cells, cols)
        if open_row is not None:
            rows.append(open_row.close())
        if cols is None:
            raise ParserError("cannot find the transaction table")
        return rows

    @classmethod
    def interpret_tables(cls, tables: list[list[list[str]]]) -> list[RawTransaction]:
        """Rows from extracted tables. Without the summary there is no opening balance, so
        the running-balance check starts at the second row; :meth:`parse` checks the first.

        Raises:
            ParserError: no transaction-table header was found (ADR-0010 rule 5).
        """
        return to_transactions(cls._read_rows(tables), opening_paise=None)

    @classmethod
    def interpret_summary(cls, lines: Sequence[str]) -> StatementSummary:
        """Opening and closing balance and the period, each ``None`` if not readable.

        A and B print a summary row of amounts under ``Opening Balance Debit Amount ...`` /
        ``Opening Balance Dr Count ...``: the first amount is the opening balance and the last
        the closing balance (B's integer counts do not match the amount pattern). B' prints
        each label and its value on separate lines.
        """
        opening: int | None = None
        closing: int | None = None
        for i, line in enumerate(lines):
            head = _SUMMARY_HEAD_RE.search(line)
            if head is None:
                continue
            tokens = _AMOUNT_TOKEN_RE.findall(line[head.end() :])
            if i + 1 < len(lines):
                tokens += _AMOUNT_TOKEN_RE.findall(lines[i + 1])
            if len(tokens) >= 2:
                opening = parse_bank_balance(tokens[0])
                closing = parse_bank_balance(tokens[-1])
                break
        if opening is None and closing is None:
            for i, line in enumerate(lines[:-1]):
                value = _AMOUNT_TOKEN_RE.findall(lines[i + 1])
                if len(value) != 1:
                    continue
                if opening is None and _LONE_OPENING_RE.match(line):
                    opening = parse_bank_balance(value[0])
                elif closing is None and _LONE_CLOSING_RE.match(line):
                    closing = parse_bank_balance(value[0])
        period = _find_period(lines, _PERIOD_RE, ("%d/%m/%Y", "%d/%m/%y"))
        return StatementSummary(
            opening_balance_paise=opening,
            closing_balance_paise=closing,
            period_start=period[0] if period else None,
            period_end=period[1] if period else None,
        )

    @classmethod
    def parse(cls, pdf_bytes: bytes, password: str | None) -> ParsedStatement:
        decrypted = _decrypt(pdf_bytes, password)
        lines = _extract_text(decrypted)
        summary = cls.interpret_summary(lines)
        if any(_WORD_LAYOUT_RE.search(line) for line in lines):
            tables = _extract_word_table(
                decrypted, headers=(_B_LABELS, _B_PRIME_LABELS), stop_re=_STOP_RE
            )
        else:
            tables = _extract_tables(decrypted)
        rows = to_transactions(cls._read_rows(tables), opening_paise=summary.opening_balance_paise)
        return ParsedStatement(rows=rows, summary=summary)
