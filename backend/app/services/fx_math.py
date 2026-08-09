"""Native→INR money conversion (PRD §F7 FX layer) — one helper, no I/O.

Every paise that rolls up to a top-line metric must be **INR paise**. This converts a
native-currency magnitude (US cents for a USD holding, paise for INR) to INR paise using a
``fx_rate_to_inr`` (INR-per-1-USD, 6dp ``Decimal``):

    inr_paise = native_paise × rate     # cents × (INR/USD) → INR paise (×100 scale cancels)

Rounded once, ``ROUND_HALF_EVEN``, in ``Decimal`` (the repo money idiom — never float). The
``rate == 1`` short-circuit is the **INR backward-compatibility guarantee**: an INR row carries
``fx_rate_to_inr == 1``, so conversion is an exact identity and an all-INR portfolio is
byte-identical to the pre-FX behaviour. Callers convert **per row, then sum** (each row may carry
a different historical rate — there is no single rate to factor out of a summed total).
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal


def to_inr_paise(native_paise: int, fx_rate_to_inr: Decimal) -> int:
    """Convert native paise to INR paise at ``fx_rate_to_inr``. ``rate == 1`` is an exact no-op."""
    if fx_rate_to_inr == 1:
        return native_paise
    return int((Decimal(native_paise) * fx_rate_to_inr).to_integral_value(rounding=ROUND_HALF_EVEN))
