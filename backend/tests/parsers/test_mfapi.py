"""Tests for the mfapi NAV-history parser (PRD §F8 view 5 benchmark backfill).

* Snapshot test parses a committed trimmed mfapi response (weekend gaps included)
  against frozen expected JSON — exercising date (DD-MM-YYYY) and NAV parsing plus
  source-order (newest-first) preservation in one shot.
* Unit tests cover the non-numeric / non-positive NAV and bad-date skip-with-warning
  paths, the bad-JSON / non-object / empty-data / all-unusable guards.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from app.parsers import MfApiParseError, parse_mfapi_navs

FIXTURES = Path(__file__).parent.parent / "fixtures" / "mfapi"


def _serialize(rows: list[Any]) -> list[dict[str, Any]]:
    """Render MfApiNavRow rows into the JSON-snapshot shape (Decimal / date as str)."""
    out: list[dict[str, Any]] = []
    for r in rows:
        d = asdict(r)
        d["nav"] = str(r.nav)
        d["nav_date"] = r.nav_date.isoformat()
        out.append(d)
    return out


def _mfapi(*entries: tuple[str, str]) -> bytes:
    payload = {
        "meta": {"scheme_code": 120716},
        "data": [{"date": d, "nav": n} for d, n in entries],
        "status": "SUCCESS",
    }
    return json.dumps(payload).encode("utf-8")


def test_mfapi_snapshot() -> None:
    raw = (FIXTURES / "scheme_120716.json").read_bytes()
    expected = json.loads((FIXTURES / "scheme_120716.expected.json").read_text("utf-8"))
    rows, warnings = parse_mfapi_navs(raw)
    assert warnings == []
    assert _serialize(rows) == expected


def test_source_order_preserved_newest_first() -> None:
    rows, _ = parse_mfapi_navs(_mfapi(("19-06-2026", "245.6789"), ("18-06-2026", "244.12")))
    assert [r.nav_date.isoformat() for r in rows] == ["2026-06-19", "2026-06-18"]


def test_non_numeric_nav_skipped_with_warning() -> None:
    rows, warnings = parse_mfapi_navs(_mfapi(("19-06-2026", "245.6789"), ("18-06-2026", "N.A.")))
    assert [r.nav_date.isoformat() for r in rows] == ["2026-06-19"]
    assert warnings == ["mfapi 18-06-2026: non-numeric nav"]


def test_non_positive_nav_skipped_with_warning() -> None:
    rows, warnings = parse_mfapi_navs(_mfapi(("19-06-2026", "245.6789"), ("17-06-2026", "0.00000")))
    assert [r.nav_date.isoformat() for r in rows] == ["2026-06-19"]
    assert warnings == ["mfapi 17-06-2026: non-positive nav"]


def test_unparseable_date_skipped_with_warning() -> None:
    # ISO date, not DD-MM-YYYY → strptime fails (day field would be "2026").
    rows, warnings = parse_mfapi_navs(_mfapi(("19-06-2026", "245.6789"), ("2026-06-18", "100.0")))
    assert [r.nav_date.isoformat() for r in rows] == ["2026-06-19"]
    assert warnings == ["mfapi 2026-06-18: unparseable date"]


def test_bad_json_raises() -> None:
    with pytest.raises(MfApiParseError, match="JSON"):
        parse_mfapi_navs(b"not json{")


def test_non_object_payload_raises() -> None:
    with pytest.raises(MfApiParseError, match="JSON object"):
        parse_mfapi_navs(b"[1, 2, 3]")


def test_empty_data_raises() -> None:
    with pytest.raises(MfApiParseError, match="no NAV history"):
        parse_mfapi_navs(_mfapi())


def test_all_rows_unusable_raises() -> None:
    with pytest.raises(MfApiParseError, match="no usable NAV rows"):
        parse_mfapi_navs(_mfapi(("19-06-2026", "N.A."), ("18-06-2026", "0")))
