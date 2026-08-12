"""Axis Bank credit-card PDF statement parser.

Uses the shared layout-detective pipeline in :mod:`app.parsers.base`. Axis
statements are text-based PDFs with a single amount column carrying a ``CR``
suffix for credits; dates appear as ``DD/MM/YYYY`` or ``DD-MM-YYYY``. If a
real statement turns out to use a different format the date-formats tuple
and the classifier regexes below are the only knobs.

**Flipkart co-branded variant.** The Axis Flipkart card statement is a
5-column table — ``[DATE, TRANSACTION DETAILS, MERCHANT CATEGORY, AMOUNT,
CASHBACK EARNED]`` — dispatched to this same parser (the account ``issuer``
is ``"axis"``; "Flipkart" is only the display name). The generic detective
in :mod:`app.parsers.base` joins *every* non-date/non-amount cell into the
merchant string, so on this layout the MCC category ("DEPT STORES") and the
per-row cashback ("30.00 Cr", which itself parses as an amount) leak into
``merchant_raw``. When the layout is detected, :func:`_to_canonical_flipkart`
rewrites each transaction row to the plain ``[date, merchant, amount]`` shape
— dropping the category and cashback columns — before handing it to the shared
detective, which then interprets it identically to a plain Axis row. Per-row
cashback is intentionally discarded (real cashback lands as its own credit
rows); the category is discarded for now (an auto-tag hint is a deferred
follow-up).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import ClassVar

from app.parsers.base import (
    ParsedStatement,
    RawTransaction,
    StatementSummary,
    TxnType,
    _decrypt,
    _extract_tables,
    _extract_text,
    _find_labelled_amount,
    _find_period,
    _interpret_tables,
    _try_parse_amount,
    _try_parse_date,
)

_PAYMENT_RE = re.compile(r"PAYMENT\s+RECEIVED|THANK\s+YOU\s+FOR\s+PAYMENT", re.IGNORECASE)
_REFUND_RE = re.compile(r"REFUND|REVERSAL|CHARGEBACK", re.IGNORECASE)
_NON_PURCHASE_DEBIT_RE = re.compile(
    r"FINANCE\s+CHARGE|INTEREST|LATE\s+PAYMENT|SERVICE\s+CHARGE|GST|FEE\b",
    re.IGNORECASE,
)

# Header cells that only appear on the Flipkart co-branded layout. The header
# row survives table extraction as an ordinary data row (it has no parseable
# date, so the detective would otherwise just skip it).
_FLIPKART_HEADER_RE = re.compile(r"CASHBACK\s+EARNED|MERCHANT\s+CATEGORY", re.IGNORECASE)

# Summary-block labels (PRD §F4a decision 7 spike). Real Axis statements print
# the closing balance under both "Total Payment Due" (summary-box header) and
# "Total Amount Due" (restated later in the statement) — either wording closes
# the same field, never ambiguously with "Minimum Amount/Payment Due".
_PREVIOUS_BALANCE_RE = re.compile(r"Previous\s+Balance", re.IGNORECASE)
_TOTAL_DUE_RE = re.compile(r"Total\s+(?:Amount|Payment)\s+Due", re.IGNORECASE)
_STATEMENT_PERIOD_RE = re.compile(r"Statement\s+Period", re.IGNORECASE)


def _classify(description: str, is_credit: bool) -> TxnType:
    if is_credit:
        if _PAYMENT_RE.search(description):
            return "payment"
        if _REFUND_RE.search(description):
            return "refund"
        return "other"
    if _NON_PURCHASE_DEBIT_RE.search(description):
        return "other"
    return "purchase"


def _is_flipkart_layout(
    tables: Sequence[Sequence[Sequence[str]]],
    date_formats: Sequence[str],
) -> bool:
    """True when the statement is the Flipkart co-branded 5-column layout.

    Two independent signals, either sufficient:

    * a header cell naming an extra column (``MERCHANT CATEGORY`` /
      ``CASHBACK EARNED``);
    * any date-led row carrying two or more amount-parseable cells (the
      ``AMOUNT`` + ``CASHBACK EARNED`` pair) — a structural fallback for
      statements whose header wording differs or whose header row didn't
      extract cleanly.
    """
    for page in tables:
        for row in page:
            if any(_FLIPKART_HEADER_RE.search(cell) for cell in row):
                return True
            if any(_try_parse_date(cell, date_formats) is not None for cell in row):
                amount_cells = sum(1 for cell in row if _try_parse_amount(cell) is not None)
                if amount_cells >= 2:
                    return True
    return False


def _reshape_flipkart_row(row: Sequence[str], date_formats: Sequence[str]) -> list[str] | None:
    """Rewrite one Flipkart 5-column row to ``[date, merchant, amount]``.

    Returns ``None`` for non-transaction rows (no parseable date, no amount
    after the date, or no text between them) — the caller passes those through
    unchanged for the shared detective to filter structurally.

    Column logic: the amount is the *first* amount-parseable cell after the
    date (the ``AMOUNT`` column; the trailing ``CASHBACK EARNED`` is a later
    cell and is excluded by construction). The merchant is the text strictly
    between the date and the amount; when there is more than one such cell the
    last one is the ``MERCHANT CATEGORY`` and is dropped (credit rows have an
    empty category cell, leaving a single text cell = the merchant).
    """
    date_idx: int | None = None
    for i, cell in enumerate(row):
        if _try_parse_date(cell, date_formats) is not None:
            date_idx = i
            break
    if date_idx is None:
        return None

    amount_idx: int | None = None
    for i in range(date_idx + 1, len(row)):
        if _try_parse_amount(row[i]) is not None:
            amount_idx = i
            break
    if amount_idx is None:
        return None

    text_cells = [
        c.strip()
        for i, c in enumerate(row)
        if date_idx < i < amount_idx and c.strip() and _try_parse_date(c, date_formats) is None
    ]
    if not text_cells:
        return None
    merchant = " ".join(text_cells[:-1]) if len(text_cells) >= 2 else text_cells[0]
    if not merchant:
        return None
    return [row[date_idx], merchant, row[amount_idx]]


def _to_canonical_flipkart(
    tables: list[list[list[str]]],
    date_formats: Sequence[str],
) -> list[list[list[str]]]:
    """Rewrite Flipkart transaction rows to the plain ``[date, merchant, amount]``
    shape so the shared detective handles them exactly like a 3-column Axis row.

    Non-transaction rows (headers, cardholder line, summary, end-of-statement)
    reshape to ``None`` and pass through unchanged.
    """
    canonical: list[list[list[str]]] = []
    for page in tables:
        new_page: list[list[str]] = []
        for row in page:
            reshaped = _reshape_flipkart_row(row, date_formats)
            new_page.append(reshaped if reshaped is not None else list(row))
        canonical.append(new_page)
    return canonical


class AxisCC:
    """Axis Bank credit-card statement parser."""

    DATE_FORMATS: ClassVar[tuple[str, ...]] = (
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d/%m/%y",
        "%d-%m-%y",
    )

    @classmethod
    def parse(cls, pdf_bytes: bytes, password: str | None) -> ParsedStatement:
        decrypted = _decrypt(pdf_bytes, password)
        pages = _extract_tables(decrypted)
        rows = cls.interpret_tables(pages)
        lines = _extract_text(decrypted)
        summary = cls.interpret_summary(lines)
        return ParsedStatement(rows=rows, summary=summary)

    @classmethod
    def interpret_tables(cls, tables: list[list[list[str]]]) -> list[RawTransaction]:
        if _is_flipkart_layout(tables, cls.DATE_FORMATS):
            tables = _to_canonical_flipkart(tables, cls.DATE_FORMATS)
        return _interpret_tables(tables, cls.DATE_FORMATS, _classify)

    @classmethod
    def interpret_summary(cls, lines: Sequence[str]) -> StatementSummary:
        opening = _find_labelled_amount(lines, _PREVIOUS_BALANCE_RE)
        closing = _find_labelled_amount(lines, _TOTAL_DUE_RE)
        period = _find_period(lines, _STATEMENT_PERIOD_RE, cls.DATE_FORMATS)
        return StatementSummary(
            # Same sign rule as _interpret_row: a credit marker (CR) flips the
            # printed magnitude positive, otherwise it's a debit (negative).
            opening_balance_paise=(
                None if opening is None else (opening[0] if opening[1] else -opening[0])
            ),
            closing_balance_paise=(
                None if closing is None else (closing[0] if closing[1] else -closing[0])
            ),
            period_start=period[0] if period is not None else None,
            period_end=period[1] if period is not None else None,
        )
