"""Statement parsers — one module per source (per PRD § F1).

Public surface for importers downstream (import_service, API routes):

* :class:`StatementParser` — the contract every parser satisfies.
* :class:`RawTransaction` — a single parsed row.
* :data:`TxnType` / :data:`AccountType` — Literal aliases.
* :class:`ParserError`, :class:`InvalidPasswordError` — exception hierarchy.
* :class:`AxisCC`, :class:`IciciCC` — concrete spend-statement parsers.
* :func:`parse_investment_csv` + :class:`ParsedInvestmentRow` / :class:`CSVParseError`
  — canonical investment-transaction CSV parser (PRD §F7).
* :func:`parse_navall` + :class:`AmfiNavRow` / :class:`AmfiParseError` — AMFI NAVAll
  mutual-fund NAV parser (PRD §F7 NAV snapshot).
* :func:`parse_mfapi_navs` + :class:`MfApiNavRow` / :class:`MfApiParseError` — mfapi.in
  NAV-history parser (PRD §F8 view 5 benchmark backfill).
"""

from app.parsers.amfi_navall import AmfiNavRow, AmfiParseError, parse_navall
from app.parsers.axis_cc import AxisCC
from app.parsers.base import (
    AccountType,
    InvalidPasswordError,
    ParserError,
    RawTransaction,
    StatementParser,
    TxnType,
)
from app.parsers.frankfurter import FrankfurterParseError, FxRateRow, parse_frankfurter_rates
from app.parsers.icici_cc import IciciCC
from app.parsers.investment_csv import CSVParseError, ParsedInvestmentRow, parse_investment_csv
from app.parsers.mfapi import MfApiNavRow, MfApiParseError, parse_mfapi_navs

__all__ = [
    "AccountType",
    "AmfiNavRow",
    "AmfiParseError",
    "AxisCC",
    "CSVParseError",
    "FrankfurterParseError",
    "FxRateRow",
    "IciciCC",
    "InvalidPasswordError",
    "MfApiNavRow",
    "MfApiParseError",
    "ParsedInvestmentRow",
    "ParserError",
    "RawTransaction",
    "StatementParser",
    "TxnType",
    "parse_frankfurter_rates",
    "parse_investment_csv",
    "parse_mfapi_navs",
    "parse_navall",
]
