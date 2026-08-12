"""ICICI Bank credit-card PDF statement parser.

Uses the shared layout-detective pipeline in :mod:`app.parsers.base`. ICICI
statements commonly carry two date columns (transaction date + posting
date); the detective grabs the first parseable date which is the
transaction date — that's what we want for reporting and fingerprinting.
Amounts use the same single-column-with-``CR`` convention as Axis. If a
real statement diverges, classifier regexes are the first tuning knob.
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
)

_PAYMENT_RE = re.compile(
    r"PAYMENT\s+RECEIVED|PAYMENT\s+THANK\s+YOU|AUTOPAY\s+PAYMENT",
    re.IGNORECASE,
)
_REFUND_RE = re.compile(r"REFUND|REVERSAL|CHARGEBACK", re.IGNORECASE)
_NON_PURCHASE_DEBIT_RE = re.compile(
    r"FINANCE\s+CHARGE|INTEREST|LATE\s+PAYMENT|SERVICE\s+TAX|GST|FEE\b|CHARGE\b",
    re.IGNORECASE,
)

# Summary-block labels. Unlike Axis's, ICICI's exact wording is unverified — no
# real ICICI PDF is available on this box (balance-reconciliation plan decision
# 8); this pattern is the sentinel-fixture guess, degrading to a spurious warn
# (never a block) if a real statement diverges.
_PREVIOUS_BALANCE_RE = re.compile(r"Previous\s+Balance", re.IGNORECASE)
_TOTAL_DUE_RE = re.compile(r"Total\s+Amount\s+Due", re.IGNORECASE)
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


class IciciCC:
    """ICICI Bank credit-card statement parser."""

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
