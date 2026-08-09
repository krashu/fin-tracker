"""frankfurter.app FX-rate parser (PRD §F7 FX layer).

Pure: ``bytes`` → ``list[FxRateRow]`` + per-row skip warnings. No DB, no logging, no
network — the caller (``fx_service``) fetches the JSON and owns I/O.

frankfurter serves a USD→INR rate either as a single observation or a date range::

    GET /latest?from=USD&to=INR       → {"date":"2026-06-23","rates":{"INR":83.5}}
    GET /2026-01-02?from=USD&to=INR   → {"date":"2026-01-02","rates":{"INR":83.1}}
        (echoes the nearest business day ≤ the requested date in "date")
    GET /2026-01-01..2026-01-10?...   → {"start_date":...,"rates":{"2026-01-02":{"INR":83.1}, ...}}
        (business days only — weekends/holidays absent)

The two shapes are told apart by the ``rates`` values: a number (single) vs a nested dict
(range). Numbers are parsed exactly via ``json.loads(parse_float=Decimal)`` (no float
round-trip — the same precision discipline as the scaled-int money columns). A non-numeric
or non-positive rate is skipped with a warning (a 0/negative rate would corrupt conversion).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.parsers.base import ParserError

_RATE_DATE_FORMAT = "%Y-%m-%d"


class FrankfurterParseError(ParserError):
    """File-level frankfurter failure: not JSON, wrong shape, or zero usable rows.

    The caller (``fx_service``) treats it like a fetch failure (rates left untouched),
    distinct from a per-row skip warning.
    """


@dataclass(frozen=True, slots=True)
class FxRateRow:
    """One currency-pair rate on one date (the pair is the caller's request context)."""

    rate_date: date
    rate: Decimal


def parse_frankfurter_rates(
    raw: bytes, *, to_currency: str = "INR"
) -> tuple[list[FxRateRow], list[str]]:
    """Parse a frankfurter response (single or range) into rows + skip warnings.

    Warnings are ``"frankfurter <raw-date>: <reason>"`` (dates/rates are public reference data).

    Raises:
        FrankfurterParseError: the body isn't a JSON object, has no ``rates``, or yielded
            no usable rows — the caller treats this as a source failure.
    """
    try:
        payload = json.loads(raw, parse_float=Decimal)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise FrankfurterParseError("frankfurter response is not valid JSON") from e
    if not isinstance(payload, dict):
        raise FrankfurterParseError("frankfurter response is not a JSON object")
    rates = payload.get("rates")
    if not isinstance(rates, dict) or not rates:
        raise FrankfurterParseError("frankfurter response has no rates")

    rows: list[FxRateRow] = []
    warnings: list[str] = []
    if to_currency in rates and not isinstance(rates[to_currency], dict):
        # Single-observation shape: {"date": "...", "rates": {"INR": 83.5}}.
        _append_row(rows, warnings, str(payload.get("date", "")).strip(), rates.get(to_currency))
    else:
        # Range shape: {"rates": {"YYYY-MM-DD": {"INR": 83.1}, ...}}.
        for date_raw, day in rates.items():
            if not isinstance(day, dict):
                warnings.append(f"frankfurter {date_raw}: malformed rate entry")
                continue
            _append_row(rows, warnings, str(date_raw).strip(), day.get(to_currency))

    if not rows:
        raise FrankfurterParseError("no usable rates in frankfurter response")
    return rows, warnings


def _append_row(rows: list[FxRateRow], warnings: list[str], date_raw: str, value: object) -> None:
    """Coerce one (date, rate) pair onto ``rows`` or record a PII-safe skip warning."""
    if value is None:
        warnings.append(f"frankfurter {date_raw}: missing rate")
        return
    try:
        rate = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation:
        warnings.append(f"frankfurter {date_raw}: non-numeric rate")
        return
    if rate <= 0:
        warnings.append(f"frankfurter {date_raw}: non-positive rate")
        return
    try:
        rate_date = datetime.strptime(date_raw, _RATE_DATE_FORMAT).date()
    except ValueError:
        warnings.append(f"frankfurter {date_raw}: unparseable date")
        return
    rows.append(FxRateRow(rate_date=rate_date, rate=rate))
