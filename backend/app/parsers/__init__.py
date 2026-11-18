"""Statement parsers — one module per source (per PRD § F1).

Public surface for importers downstream (import_service, API routes):

* :class:`StatementParser` — the contract every parser satisfies.
* :class:`RawTransaction` — a single parsed row.
* :class:`StatementSummary` — statement-level metadata (opening/closing balance, period).
* :class:`ParsedStatement` — everything one statement file yields: rows plus a summary.
* :data:`TxnType` / :data:`AccountType` — Literal aliases.
* :class:`ParserError`, :class:`InvalidPasswordError`, :class:`RunningBalanceMismatchError` —
  exception hierarchy.
* :class:`AxisCC`, :class:`AxisBank`, :class:`IciciCC`, :class:`HdfcBank`, :class:`IciciBank`,
  :class:`SbiBank` — concrete spend-statement parsers.
* :func:`parse_investment_csv` + :class:`ParsedInvestmentRow` / :class:`CSVParseError`
  — canonical investment-transaction CSV parser (PRD §F7).
* :func:`parse_navall` + :class:`AmfiNavRow` / :class:`AmfiParseError` — AMFI NAVAll
  mutual-fund NAV parser (PRD §F7 NAV snapshot).
* :func:`parse_mfapi_navs` + :class:`MfApiNavRow` / :class:`MfApiParseError` — mfapi.in
  NAV-history parser (PRD §F8 view 5 benchmark backfill).
"""

from app.parsers.amfi_navall import AmfiNavRow, AmfiParseError, parse_navall
from app.parsers.axis_bank import AxisBank
from app.parsers.axis_cc import AxisCC
from app.parsers.base import (
    AccountType,
    InvalidPasswordError,
    ParsedStatement,
    ParserError,
    RawTransaction,
    RunningBalanceMismatchError,
    StatementParser,
    StatementSummary,
    TxnType,
)
from app.parsers.frankfurter import FrankfurterParseError, FxRateRow, parse_frankfurter_rates
from app.parsers.hdfc_bank import HdfcBank
from app.parsers.icici_bank import IciciBank
from app.parsers.icici_cc import IciciCC
from app.parsers.investment_csv import (
    CSVParseError,
    ParsedInvestmentRow,
    UnsupportedFileFormatError,
    parse_investment_csv,
    parse_investment_file,
)
from app.parsers.mfapi import MfApiNavRow, MfApiParseError, parse_mfapi_navs
from app.parsers.sbi_bank import SbiBank

__all__ = [
    "AccountType",
    "AmfiNavRow",
    "AmfiParseError",
    "AxisBank",
    "AxisCC",
    "CSVParseError",
    "FrankfurterParseError",
    "FxRateRow",
    "HdfcBank",
    "IciciBank",
    "IciciCC",
    "InvalidPasswordError",
    "MfApiNavRow",
    "MfApiParseError",
    "ParsedInvestmentRow",
    "ParsedStatement",
    "ParserError",
    "RawTransaction",
    "RunningBalanceMismatchError",
    "SbiBank",
    "StatementParser",
    "StatementSummary",
    "TxnType",
    "UnsupportedFileFormatError",
    "parse_frankfurter_rates",
    "parse_investment_csv",
    "parse_investment_file",
    "parse_mfapi_navs",
    "parse_navall",
]
