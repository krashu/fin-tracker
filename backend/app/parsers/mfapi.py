"""mfapi.in NAV-history parser (PRD §F8 view 5 — benchmark backfill).

Pure: ``bytes`` → ``list[MfApiNavRow]`` + per-row skip warnings. No DB, no logging,
no network — the caller (``benchmark_service``) fetches the JSON and owns I/O.

mfapi serves one mutual fund's full NAV history at ``api.mfapi.in/mf/<scheme_code>``::

    {"meta": {... "scheme_code": 120716 ...},
     "data": [{"date": "19-06-2026", "nav": "245.6789"}, ...],   <- newest-first
     "status": "SUCCESS"}

Date format is **DD-MM-YYYY** (NOT AMFI NAVAll's ``%d-%b-%Y``). NAV strings → ``Decimal``;
a non-numeric or **non-positive** NAV (mfapi emits ``0.00000`` for some pre-listing dates)
is skipped with a warning — a zero NAV would divide-by-zero the counterfactual replay.
Source order (newest-first) is preserved; the service sorts ascending for forward pricing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.parsers.base import ParserError

_NAV_DATE_FORMAT = "%d-%m-%Y"


class MfApiParseError(ParserError):
    """File-level mfapi failure: not JSON, wrong shape, or zero usable NAV rows.

    The caller (``benchmark_service``) treats it like a fetch failure for that scheme
    (its NAV history left untouched), distinct from a per-row skip warning.
    """


@dataclass(frozen=True, slots=True)
class MfApiNavRow:
    """One day's NAV for a single fund."""

    nav_date: date
    nav: Decimal


def parse_mfapi_navs(raw: bytes) -> tuple[list[MfApiNavRow], list[str]]:
    """Parse an mfapi NAV-history body into rows + skip warnings.

    Warnings are ``"mfapi <raw-date>: <reason>"`` (dates/NAVs are public reference data).

    Raises:
        MfApiParseError: the body isn't a JSON object, has no ``data`` list, or yielded
            no usable rows — the caller treats this as a source failure for the scheme.
    """
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise MfApiParseError("mfapi response is not valid JSON") from e
    if not isinstance(payload, dict):
        raise MfApiParseError("mfapi response is not a JSON object")
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise MfApiParseError("mfapi response has no NAV history")

    rows: list[MfApiNavRow] = []
    warnings: list[str] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        date_raw = str(entry.get("date", "")).strip()
        nav_raw = str(entry.get("nav", "")).strip().replace(",", "")
        try:
            nav = Decimal(nav_raw)
        except InvalidOperation:
            warnings.append(f"mfapi {date_raw}: non-numeric nav")
            continue
        if nav <= 0:
            # mfapi emits 0.00000 for some pre-listing dates; a 0 NAV would
            # divide-by-zero the benchmark unit math, so drop it.
            warnings.append(f"mfapi {date_raw}: non-positive nav")
            continue
        try:
            nav_date = datetime.strptime(date_raw, _NAV_DATE_FORMAT).date()
        except ValueError:
            warnings.append(f"mfapi {date_raw}: unparseable date")
            continue
        rows.append(MfApiNavRow(nav_date=nav_date, nav=nav))

    if not rows:
        raise MfApiParseError("no usable NAV rows in mfapi response")
    return rows, warnings
