"""StatementParser protocol — common shape every parser implements.

Public surface re-exported by ``app.parsers``:

* :class:`RawTransaction` — one parsed row (frozen, with sign invariant).
* :class:`StatementSummary` — statement-level metadata (opening/closing balance,
  period); every field independently optional.
* :class:`ParsedStatement` — everything one statement file yields: ``rows`` plus
  a ``summary``.
* :data:`TxnType` / :data:`AccountType` — narrow Literal aliases.
* :class:`StatementParser` — Protocol every per-issuer parser satisfies.
* :class:`ParserError` / :class:`InvalidPasswordError` — exception hierarchy.
* :func:`parse_statement` — the one call site's wrapper: any exception a parser
  raises that is not already a :class:`ParserError` becomes one (ADR-0010 rule 13).

Private helpers used by every concrete parser in this package:

* :func:`_decrypt` — pikepdf round-trip that strips the password.
* :func:`_extract_tables` — pdfplumber page→table→row→cell extraction.
* :func:`_extract_word_table` — positioned-word reader yielding the same
  ``pages → rows → cells`` shape, for a layout with no ruled table (ADR-0010 rule 4).
* :func:`_extract_text` — pdfplumber page-text extraction, split into lines;
  feeds ``interpret_summary``.
"""

from __future__ import annotations

import re
import string
from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from io import BytesIO
from typing import Any, Literal, Protocol

import pdfplumber
import pikepdf

from app.core.log_config import get_logger

logger = get_logger(__name__)

TxnType = Literal["purchase", "payment", "refund", "other", "withdrawal", "deposit"]
AccountType = Literal["credit_card", "bank"]


@dataclass(frozen=True, slots=True)
class RawTransaction:
    """A single parsed transaction row from a statement.

    ``amount_paise`` is signed in the user's position, the same convention as the
    summary balances: a row that lowers the position (a card purchase) is negative, one
    that raises it (a card payment or refund) is positive. ``other`` (interest, fees,
    statement charges that don't fit a cleaner bucket yet) carries no sign constraint.

    ``withdrawal`` and ``deposit`` are the bank column types: the column a row printed its
    amount in, never a guess from the balance (ADR-0010 rule 12). A withdrawal is
    ``<= 0`` and a deposit ``>= 0``; ``refund`` is the same ``>= 0`` type a bank parser
    gives a credit it recognises as a reversal.
    """

    date: date
    amount_paise: int
    merchant_raw: str
    txn_type: TxnType

    def __post_init__(self) -> None:
        if self.txn_type in {"purchase", "withdrawal"} and self.amount_paise > 0:
            raise ValueError(
                f"{self.txn_type} must have amount_paise <= 0, got {self.amount_paise}"
            )
        if self.txn_type in {"payment", "refund", "deposit"} and self.amount_paise < 0:
            raise ValueError(
                f"{self.txn_type} must have amount_paise >= 0, got {self.amount_paise}"
            )


@dataclass(frozen=True, slots=True)
class StatementSummary:
    """Statement-level metadata: opening/closing balance and billing period.

    Balances are the user's position (ADR-0010 rule 7): positive is money held, negative is
    owed. A card prints what is owed bare, so it is stored negative unless the statement says
    ``CR``; a bank prints what is held bare, so it is stored positive unless ``-`` or ``DR``
    marks an overdraft. Either way ``opening + Σ rows = closing``.

    Every field is independently optional — a layout that prints no summary
    block (or one a parser can't read) yields an all-``None`` instance, never
    ``None`` itself. That removes a ``None``-of-``None`` double check at every
    read site.
    """

    opening_balance_paise: int | None = None
    closing_balance_paise: int | None = None
    period_start: date | None = None
    period_end: date | None = None


@dataclass(frozen=True, slots=True)
class ParsedStatement:
    """Everything one statement file yields: rows plus statement-level metadata.

    Zero rows is a valid statement when any summary field is set — a month with
    no spend still prints its balances, and reconciliation should confirm them
    (ADR-0010 rule 5). No rows **and** an all-``None`` summary is nothing to
    import, so construction raises :class:`ParserError`.
    """

    rows: list[RawTransaction]
    summary: StatementSummary

    def __post_init__(self) -> None:
        if not self.rows and self.summary == StatementSummary():
            raise ParserError("no transactions found")


class StatementParser(Protocol):
    """Contract every per-issuer parser implements.

    Concrete parsers are classes with three classmethods, all declared here so
    ``ty`` rejects a parser missing one (ADR-0010 rule 1):

    * :meth:`parse` — decrypts once and composes the other two, returning a
      :class:`ParsedStatement` whose rows are ordered by
      ``(date, page_index, row_index)``. Zero rows is valid when the summary
      has any field set; with an all-``None`` summary too it raises
      :class:`ParserError` (ADR-0010 rule 5).
    * :meth:`interpret_tables` — extracted ``pages → rows → cells`` to rows.
    * :meth:`interpret_summary` — extracted text lines to a
      :class:`StatementSummary`; all-``None`` when no summary block is readable.

    Dispatch keys off the account's ``(issuer, type)`` DB columns via the
    ``PARSERS`` table in :mod:`app.services.import_service` — parsers don't
    carry their own issuer/type metadata.
    """

    @classmethod
    def parse(cls, pdf_bytes: bytes, password: str | None) -> ParsedStatement: ...

    @classmethod
    def interpret_tables(cls, tables: list[list[list[str]]]) -> list[RawTransaction]: ...

    @classmethod
    def interpret_summary(cls, lines: Sequence[str]) -> StatementSummary: ...


class ParserError(Exception):
    """Generic parser failure: malformed PDF, unrecognised layout, or no rows
    and no summary."""


class InvalidPasswordError(ParserError):
    """The supplied password did not decrypt the PDF."""


class RunningBalanceMismatchError(ParserError):
    """A bank row's printed balance is not the previous balance plus its signed amount.

    ``page`` and ``row`` are 1-based: the PDF page, and the transaction's ordinal on that
    page. They are the whole payload: the message names a position and never statement text,
    so the route can return and log it (ADR-0010 rule 11).
    """

    def __init__(self, *, page: int, row: int) -> None:
        self.page = page
        self.row = row
        super().__init__(f"running balance does not match on page {page}, row {row}")


def parse_statement(
    parser: type[StatementParser], pdf_bytes: bytes, password: str | None
) -> ParsedStatement:
    """Run ``parser.parse``; any exception that is not a :class:`ParserError` becomes one.

    A parser bug (an ``IndexError`` on a layout it did not expect, a ``ValueError`` from a
    row invariant) would otherwise escape the upload route as a 500 whose logged traceback
    can quote statement text. Only the parser name and the exception *type* are logged, and
    the replacement ``ParserError`` carries a fixed message. A :class:`ParserError`
    (including its subclasses) passes through unchanged.

    ``from None`` sets ``__suppress_context__``, so a traceback does not print the original.
    The original still sits on ``__context__``; nothing in this codebase reads it.
    """
    try:
        return parser.parse(pdf_bytes, password)
    except ParserError:
        raise
    except Exception as e:
        logger.error(
            "statement_parser_crashed", parser=parser.__name__, error_type=type(e).__name__
        )
        raise ParserError("statement parser failed") from None


def _decrypt(pdf_bytes: bytes, password: str | None) -> bytes:
    """Decrypt a password-protected PDF in memory.

    Returns the decrypted PDF bytes. If the PDF is not encrypted the input
    is re-serialised unchanged (pikepdf round-trip — cheap and means the
    downstream extractor never has to handle two cases).

    Raises:
        InvalidPasswordError: wrong or missing password on an encrypted PDF.
        ParserError: the bytes are not a parseable PDF.
    """
    try:
        with pikepdf.open(BytesIO(pdf_bytes), password=password or "") as pdf:
            buf = BytesIO()
            pdf.save(buf)
            return buf.getvalue()
    except pikepdf.PasswordError as e:
        raise InvalidPasswordError("incorrect or missing PDF password") from e
    except Exception as e:
        raise ParserError(f"could not open PDF: {e}") from e


def _extract_tables(decrypted_bytes: bytes) -> list[list[list[str]]]:
    """Extract tables from a decrypted PDF as ``pages → rows → cells``.

    All tables on a single page are concatenated row-wise; table boundaries
    within a page are not preserved because ``interpret_tables`` filters
    non-transaction rows structurally (by date-parseability of column 0)
    rather than by table identity. ``None`` cells become ``""``.

    Raises:
        ParserError: pdfplumber could not open the PDF or table extraction
            failed for any reason.
    """
    try:
        pages: list[list[list[str]]] = []
        with pdfplumber.open(BytesIO(decrypted_bytes)) as pdf:
            for page in pdf.pages:
                page_rows: list[list[str]] = []
                for table in page.extract_tables() or []:
                    for row in table:
                        page_rows.append([cell or "" for cell in row])
                pages.append(page_rows)
        return pages
    except Exception as e:
        raise ParserError(f"failed to extract tables: {e}") from e


def _extract_text(decrypted_bytes: bytes) -> list[str]:
    """Extract page text from a decrypted PDF, split into lines and concatenated
    across pages in page order.

    ``page.extract_text()`` is a practical superset of table text, so a summary
    block (opening/closing balance, statement period) prints here whether or not
    it also landed in a detected table — this is what :func:`_find_labelled_amount`
    and :func:`_find_period` scan.

    Raises:
        ParserError: pdfplumber could not open the PDF or text extraction failed.
    """
    try:
        lines: list[str] = []
        with pdfplumber.open(BytesIO(decrypted_bytes)) as pdf:
            for page in pdf.pages:
                lines.extend((page.extract_text() or "").splitlines())
        return lines
    except Exception as e:
        raise ParserError(f"failed to extract text: {e}") from e


# One line of a page: words whose ``top`` is within this many points of the line's first word.
_LINE_TOLERANCE = 3.0


def _normalise_label(text: str) -> str:
    """Casefold, drop whitespace and a trailing ``*`` so ``Closing Balance*`` and a
    compressed ``ClosingBalance`` compare equal."""
    return "".join(text.split()).casefold().rstrip("*")


def _match_header(
    words: list[dict[str, Any]], labels: Sequence[str]
) -> list[tuple[float, float, str]] | None:
    """Locate ``labels``, in order, among a line's words.

    Each label may span several consecutive words (``Withdrawal Amt.`` is two) or sit inside
    one (``WithdrawalAmt.``). Returns one ``(x0, x1, printed text)`` per label, or ``None``.
    """
    for start in range(len(words)):
        spans: list[tuple[float, float, str]] = []
        i = start
        for label in labels:
            target = _normalise_label(label)
            first = i
            acc = ""
            while i < len(words) and len(acc) < len(target):
                acc += _normalise_label(words[i]["text"])
                i += 1
            if not target or acc != target:
                break
            spans.append(
                (
                    words[first]["x0"],
                    words[i - 1]["x1"],
                    " ".join(w["text"] for w in words[first:i]),
                )
            )
        else:
            return spans
    return None


def _header_spans(
    line: list[dict[str, Any]], headers: Sequence[Sequence[str]]
) -> list[tuple[float, float, str]] | None:
    """The column spans when ``line`` is a header under any alternative in ``headers``."""
    for labels in headers:
        spans = _match_header(line, labels)
        if spans is not None:
            return spans
    return None


def _extract_word_table(
    decrypted_bytes: bytes,
    *,
    headers: Sequence[Sequence[str]],
    stop_re: re.Pattern[str],
) -> list[list[list[str]]]:
    """Read ``pages → rows → cells`` from positioned words, for a layout with no ruled table.

    Some statements print their transaction table as plain text, so
    :func:`_extract_tables` finds nothing and text extraction loses an empty column (a
    withdrawal and a deposit then look alike). This reader keeps the column by position
    (ADR-0010 rule 4):

    * words are clustered into lines by ``top``;
    * a line is the header when one alternative in ``headers`` matches its words in order;
      the boundary between two columns is the midpoint between one label's right edge and the
      next label's left edge, so a right-aligned amount that starts left of its own label
      still lands in that column (a word goes to the column holding its horizontal centre);
    * on a header page the lines below the header are rows; a later page with no header
      continues with the last columns seen, from the page top; pages before the first header
      are ``[]``;
    * on each page the rows end at the first line below that page's header that matches
      ``stop_re`` (a footer or the summary block). On a page with its own header nothing above
      that header is a row, and a stop line there neither ends the page nor keeps the carried
      lines above it. A page with no header reads, and stops, from its top.

    The header line itself is the first row of its page, as printed. One list per PDF page
    is always returned, which keeps the page index.

    Raises:
        ParserError: pdfplumber could not open the PDF or word extraction failed.
    """
    try:
        pages: list[list[list[str]]] = []
        boundaries: list[float] | None = None
        with pdfplumber.open(BytesIO(decrypted_bytes)) as pdf:
            for page in pdf.pages:
                words = sorted(page.extract_words(), key=lambda w: (w["top"], w["x0"]))
                lines: list[list[dict[str, Any]]] = []
                line_top = 0.0
                for word in words:
                    if not lines or word["top"] - line_top > _LINE_TOLERANCE:
                        lines.append([])
                        line_top = word["top"]
                    lines[-1].append(word)
                for line in lines:
                    line.sort(key=lambda w: w["x0"])
                header_at = [_header_spans(line, headers) for line in lines]
                first_header = next((i for i, spans in enumerate(header_at) if spans), None)
                page_rows: list[list[str]] = []
                for i, line in enumerate(lines):
                    spans = header_at[i]
                    if spans is not None:
                        boundaries = [
                            (spans[j][1] + spans[j + 1][0]) / 2 for j in range(len(spans) - 1)
                        ]
                        page_rows.append([text for _, _, text in spans])
                        continue
                    if boundaries is None:
                        continue
                    if first_header is not None and i < first_header:
                        continue
                    if stop_re.search(" ".join(w["text"] for w in line)):
                        break
                    cells = [""] * (len(boundaries) + 1)
                    for word in line:
                        centre = (word["x0"] + word["x1"]) / 2
                        col = bisect_right(boundaries, centre)
                        cells[col] = f"{cells[col]} {word['text']}".strip()
                    page_rows.append(cells)
                pages.append(page_rows)
        return pages
    except Exception as e:
        raise ParserError(f"failed to extract words: {e}") from e


def _find_labelled_amount(
    lines: Sequence[str], pattern: re.Pattern[str]
) -> tuple[int, bool] | None:
    """First line matching ``pattern``, then :func:`_try_parse_amount` on its
    trailing amount token. Returns ``(paise, is_credit)`` — sign application
    stays the caller's job, exactly as it already is for row amounts.

    The trailing token is tried two words wide before one (``"1,234.56 CR"``
    is a label-then-amount line with the credit marker split off by
    whitespace; trying the single last word first would grab bare ``"CR"``
    and fail to parse it, silently losing the marker). Lines whose matched
    label has no parseable trailing amount are skipped, not fatal — a later
    line matching the same pattern gets a turn.

    Returns ``None`` if no matching line yields a parseable amount.
    """
    for line in lines:
        if not pattern.search(line):
            continue
        tokens = line.split()
        for width in (2, 1):
            if len(tokens) < width:
                continue
            parsed = _try_parse_amount(" ".join(tokens[-width:]))
            if parsed is not None:
                return parsed
    return None


def _find_period(
    lines: Sequence[str], pattern: re.Pattern[str], date_formats: Sequence[str]
) -> tuple[date, date] | None:
    """First line matching ``pattern``; the first two of its whitespace-separated
    tokens that parse as dates (via ``date_formats``) become
    ``(period_start, period_end)``. Punctuation touching either end of a token (``:``, ``,``,
    ``(``, ``)``, ``.`` …) is stripped before parsing.

    Returns ``None`` if no matching line yields two parseable dates.
    """
    for line in lines:
        if not pattern.search(line):
            continue
        found: list[date] = []
        for token in line.split():
            parsed = _try_parse_date(token.strip(string.punctuation), date_formats)
            if parsed is not None:
                found.append(parsed)
        if len(found) >= 2:
            return found[0], found[1]
    return None


# ---------------------------------------------------------------------------
# Row-interpretation helpers (shared by every concrete parser).
#
# Each parser supplies its own date-format tuple and classifier callable;
# the row-walking + column-detective + sign-application logic is identical
# across issuers so it lives here once.
# ---------------------------------------------------------------------------


# Matches "₹1,23,456.78", "1,23,456.78 CR", "10000.00", "450", "-450.00 DR".
# Comma grouping is permissive (any positions) because issuers mix Indian
# lakh-style and Western thousand-style on the same statement; the resulting
# string has commas stripped before Decimal parsing so the actual digit
# layout doesn't matter. ``CR`` / ``DR`` markers and a leading ``-`` are
# alternative credit signals.
_AMOUNT_RE = re.compile(
    r"""^
        \s*
        ₹?\s*                       # optional rupee symbol
        (?P<sign>-)?                # optional leading minus
        (?P<num>\d[\d,]*(?:\.\d{1,2})?)  # any digit-and-comma sequence, optional .dd
        \s*
        (?P<cr>CR|Cr|cr|DR|Dr|dr)?  # optional credit/debit marker
        \s*
        $""",
    re.VERBOSE,
)


def _try_parse_date(cell: str, formats: Sequence[str]) -> date | None:
    """Try each ``strftime`` format; return the first that parses, else ``None``."""
    stripped = cell.strip()
    if not stripped:
        return None
    for fmt in formats:
        try:
            return datetime.strptime(stripped, fmt).date()
        except ValueError:
            continue
    return None


def _try_parse_amount(cell: str) -> tuple[int, bool] | None:
    """Parse an Indian-format rupee amount into ``(paise, is_credit)``.

    ``paise`` is always non-negative; sign assignment is the caller's job
    (it depends on the transaction type, not just the cell). ``is_credit``
    is ``True`` when the cell carries a ``CR`` marker or a leading minus,
    ``False`` for explicit ``DR`` or bare positive amounts.

    Returns ``None`` if the cell isn't a parseable amount.
    """
    stripped = cell.strip()
    if not stripped:
        return None
    match = _AMOUNT_RE.match(stripped)
    if not match:
        return None
    try:
        rupees = Decimal(match.group("num").replace(",", ""))
    except InvalidOperation:
        return None
    paise = int((rupees * 100).to_integral_value(rounding=ROUND_HALF_EVEN))
    marker = (match.group("cr") or "").upper()
    is_credit = marker == "CR" or (match.group("sign") == "-" and marker != "DR")
    return paise, is_credit


Classifier = Callable[[str, bool], TxnType]


def _interpret_row(
    row: Sequence[str],
    date_formats: Sequence[str],
    classify: Classifier,
) -> RawTransaction | None:
    """Interpret one table row as a transaction or return ``None``.

    Layout-detective: the first cell that parses as a date is the date
    column, the first cell after that which parses as an amount is the
    amount column, and the remaining non-empty cells join to form the
    merchant/description string. Returns ``None`` when the row isn't a
    transaction at all (header, summary, blank, "Total Outstanding", etc.).

    Assumes the amount column FOLLOWS the date column — true of both shipped issuers.
    A layout that prints the amount before the date drops the row silently (and
    :class:`ParsedStatement` only raises when no row interprets *and* no summary field
    was read), so an issuer with a trailing-date layout needs its own reshaper, the way
    Flipkart-format Axis rows do.
    """
    if not row:
        return None

    date_idx: int | None = None
    txn_date: date | None = None
    for i, cell in enumerate(row):
        parsed = _try_parse_date(cell, date_formats)
        if parsed is not None:
            date_idx = i
            txn_date = parsed
            break
    if txn_date is None or date_idx is None:
        return None

    amount_idx: int | None = None
    paise = 0
    is_credit = False
    # Scan AFTER the date column, never from index 0 — matching this function's own
    # docstring and _reshape_flipkart_row's range(date_idx+1, ...). ``_AMOUNT_RE`` accepts a
    # bare digit string, so a leading serial-number column ("1", "2", …) used to win this scan
    # and make every row a 1-rupee purchase, with the real amount joined into merchant_raw —
    # poisoning the F4 fingerprint, the F3 tag-map key and every spend total.
    for i in range(date_idx + 1, len(row)):
        parsed_amount = _try_parse_amount(row[i])
        if parsed_amount is not None:
            amount_idx = i
            paise, is_credit = parsed_amount
            break
    if amount_idx is None:
        return None

    # Skip the matched date / amount cells AND any other cell that parses as
    # a date — ICICI statements carry a second "posting date" column that
    # would otherwise leak into merchant_raw and defeat the F3 tagging
    # normaliser, which only knows how to strip dates from the trailing edge.
    desc_cells = [
        c.strip()
        for i, c in enumerate(row)
        if i not in (date_idx, amount_idx)
        and c.strip()
        and _try_parse_date(c, date_formats) is None
    ]
    description = " ".join(desc_cells)
    if not description:
        return None

    txn_type = classify(description, is_credit)
    signed_paise = paise if is_credit else -paise
    return RawTransaction(
        date=txn_date,
        amount_paise=signed_paise,
        merchant_raw=description,
        txn_type=txn_type,
    )


def _interpret_tables(
    tables: list[list[list[str]]],
    date_formats: Sequence[str],
    classify: Classifier,
) -> list[RawTransaction]:
    """Walk ``pages → rows`` and collect interpreted transactions.

    Output order: ``(date, page_index, row_index)``. Pinned so snapshot
    tests are deterministic and downstream fingerprinting is stable.

    Returns ``[]`` when no row interprets — a statement period with no
    transactions is legitimate. The guard against an issuer layout change the
    detective logic can't follow lives in :class:`ParsedStatement`: no rows
    *and* an all-``None`` summary raises there (ADR-0010 rule 5).
    """
    positioned: list[tuple[date, int, int, RawTransaction]] = []
    for page_idx, page in enumerate(tables):
        for row_idx, row in enumerate(page):
            txn = _interpret_row(row, date_formats, classify)
            if txn is not None:
                positioned.append((txn.date, page_idx, row_idx, txn))
    positioned.sort(key=lambda t: (t[0], t[1], t[2]))
    return [t[3] for t in positioned]
