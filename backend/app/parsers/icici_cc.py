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
from typing import ClassVar

from app.parsers.base import (
    RawTransaction,
    TxnType,
    _decrypt,
    _extract_tables,
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
    def parse(cls, pdf_bytes: bytes, password: str | None) -> list[RawTransaction]:
        decrypted = _decrypt(pdf_bytes, password)
        pages = _extract_tables(decrypted)
        return cls.interpret_tables(pages)

    @classmethod
    def interpret_tables(cls, tables: list[list[list[str]]]) -> list[RawTransaction]:
        return _interpret_tables(tables, cls.DATE_FORMATS, _classify)
