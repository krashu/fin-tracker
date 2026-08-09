"""AMFI NAVAll parser (PRD §F7 — mutual-fund NAV snapshot).

Pure: ``bytes`` → ``list[AmfiNavRow]`` + per-row skip warnings. No DB, no logging,
no network — the caller (``nav_snapshot_service``) fetches the file and owns I/O.

AMFI publishes every open-/closed-ended scheme's latest NAV as one semicolon-delimited
text file (``portal.amfiindia.com/spages/NAVAll.txt``). The layout is hierarchical:

    Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date
    <blank>
    Open Ended Schemes(Debt Scheme - ...)        <- category header (no ';')
    <blank>
    Aditya Birla Sun Life Mutual Fund            <- AMC header (no ';')
    <blank>
    119551;INF209KA12Z1;INF209KA13Z9;... ;105.9219;19-Jun-2026   <- data row (6 ';' fields)

A **data row** is the only thing we keep: exactly six ``;``-separated fields whose first
field is a numeric scheme code. The header, blank lines, category headers, and AMC-name
headers all fail that test and are skipped — no fragile section-state machine needed.

**Two ISIN columns.** Field 2 is the growth/payout ISIN, field 3 the dividend-reinvestment
ISIN (often ``-``). A holding's ISIN may be either, so the snapshot service indexes both.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.parsers.base import ParserError

_NAV_DATE_FORMAT = "%d-%b-%Y"


class AmfiParseError(ParserError):
    """File-level AMFI NAVAll failure: bad encoding or zero usable NAV rows.

    Maps to HTTP 422 at the route; the snapshot service treats it like a fetch
    failure (MF NAVs left untouched), distinct from a per-row skip warning.
    """


@dataclass(frozen=True, slots=True)
class AmfiNavRow:
    """One scheme's latest NAV. ``isin_growth`` = the growth/payout ISIN (field 2),
    ``isin_reinvest`` = the dividend-reinvestment ISIN (field 3); either may be ``None``."""

    scheme_code: str
    isin_growth: str | None
    isin_reinvest: str | None
    scheme_name: str
    nav: Decimal
    nav_date: date


def _isin_or_none(cell: str) -> str | None:
    """Normalise an ISIN cell — ``-`` / blank → ``None``, else upper-cased."""
    s = cell.strip().upper()
    return None if not s or s == "-" else s


def parse_navall(raw: bytes) -> tuple[list[AmfiNavRow], list[str]]:
    """Parse an AMFI NAVAll body into rows + PII-safe skip warnings.

    Warnings are ``"scheme <code>: <reason>"`` — code + reason only, never the raw
    line (scheme codes / ISINs are public reference data, but keep the shape uniform
    with the other parsers).

    Raises:
        AmfiParseError: the bytes aren't UTF-8, or no usable NAV rows were found
            (empty / garbage body — the caller treats this as a source failure).
    """
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise AmfiParseError("AMFI NAVAll is not valid UTF-8 text") from e

    rows: list[AmfiNavRow] = []
    warnings: list[str] = []
    for line in text.splitlines():
        fields = line.split(";")
        # A data row is exactly 6 ';'-fields with a numeric scheme code. The header
        # ("Scheme Code;..."), blank lines, category headers and AMC names all fail this.
        if len(fields) != 6:
            continue
        scheme_code = fields[0].strip()
        if not scheme_code.isdigit():
            continue

        nav_raw = fields[4].strip().replace(",", "")
        try:
            nav = Decimal(nav_raw)
        except InvalidOperation:
            warnings.append(f"scheme {scheme_code}: non-numeric NAV")
            continue

        try:
            nav_date = datetime.strptime(fields[5].strip(), _NAV_DATE_FORMAT).date()
        except ValueError:
            warnings.append(f"scheme {scheme_code}: unparseable date")
            continue

        rows.append(
            AmfiNavRow(
                scheme_code=scheme_code,
                isin_growth=_isin_or_none(fields[1]),
                isin_reinvest=_isin_or_none(fields[2]),
                scheme_name=fields[3].strip(),
                nav=nav,
                nav_date=nav_date,
            )
        )

    if not rows:
        raise AmfiParseError("no NAV rows found in AMFI NAVAll body")
    return rows, warnings
