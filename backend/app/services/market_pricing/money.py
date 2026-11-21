"""Fixed-point price units (ADR-0021).

Prices are integers in units of 10^-4 of the currency (``nav_micros``:
100 units = 1 paisa / 1 cent). No floats are ever stored. AMFI publishes
NAVs to 4 decimals, so this is lossless for funds.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from fractions import Fraction

SCALE = 10_000
_ONE = Decimal(1)


def to_micros(value: str | int | float | Decimal | Fraction) -> int:
    """Convert a price to integer 10^-4 units, rounding half-even."""
    if isinstance(value, Fraction):
        scaled = value * SCALE
        return int(
            (Decimal(scaled.numerator) / Decimal(scaled.denominator)).quantize(
                _ONE, rounding=ROUND_HALF_EVEN
            )
        )
    try:
        if isinstance(value, Decimal):
            dec = value
        elif isinstance(value, float):
            # repr() is the shortest round-trip form, so 131.88 stays 131.88.
            dec = Decimal(repr(value))
        else:
            dec = Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"not a price: {value!r}") from exc
    if not dec.is_finite():
        raise ValueError(f"not a finite price: {value!r}")
    return int((dec * SCALE).quantize(_ONE, rounding=ROUND_HALF_EVEN))


def from_micros(value: int) -> Decimal:
    """Integer 10^-4 units back to a Decimal with 4 places."""
    return (Decimal(value) / SCALE).quantize(Decimal("0.0001"))
