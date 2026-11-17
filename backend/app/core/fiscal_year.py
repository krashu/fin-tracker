"""Indian financial-year maths (1 April - 31 March), named by the start year (PRD §F11).

The FY is the tax year under the Income-tax Act, 2025; the spend dashboards use calendar
years instead (PRD §F8). ``parse_year`` validates both. Pure date maths; no clock:
callers pass ``today`` in.
"""

from __future__ import annotations

import re
from datetime import date

_YEAR_RE = re.compile(r"^\d{4}$")
# Bounds a shape-valid-but-nonsense year like "0000": ``_YEAR_RE`` alone accepts
# it, and ``date(0, 1, 1)`` raises ValueError - an uncontrolled 500, not a 422.
MIN_YEAR = 1900
MAX_YEAR = 2100


def fy_start_year(d: date) -> int:
    """The Indian financial year ``d`` falls in, named by its start year (PRD §F11).

    The FY runs 1 April - 31 March, so 15 March 2026 is in FY 2025-26 -> ``2025``.
    """
    return d.year if d.month >= 4 else d.year - 1


def fy_bounds(y: int) -> tuple[date, date]:
    """Inclusive bounds of the FY starting in ``y``: 1 April ``y`` - 31 March ``y + 1``."""
    return date(y, 4, 1), date(y + 1, 3, 31)


def parse_year(value: str) -> int | None:
    """The year in ``value`` (``YYYY``, within MIN_YEAR..MAX_YEAR), or ``None``.

    The caller decides what the year means: a calendar year on the dashboards, an FY
    start year on the tax routes.

    Never echoes the input: callers turn ``None`` into a generic 422.
    """
    if not _YEAR_RE.match(value):
        return None
    y = int(value)
    return y if MIN_YEAR <= y <= MAX_YEAR else None


def fy_span(min_date: date | None, max_date: date | None, today: date) -> list[int]:
    """Every FY from the earliest to the latest of ``min_date``, ``max_date`` and
    ``today``'s FY, newest first.

    One MIN/MAX pass, so a user whose rows all predate or postdate the current FY still
    gets the whole span plus the current year (never ``[]``).
    """
    current_fy = fy_start_year(today)
    start_fy = min(current_fy, fy_start_year(min_date)) if min_date else current_fy
    end_fy = max(current_fy, fy_start_year(max_date)) if max_date else current_fy
    return list(range(end_fy, start_fy - 1, -1))
