"""Unit tests for the native→INR conversion helper (PRD §F7 FX layer)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.fx_math import to_inr_paise


@pytest.mark.parametrize("n", [0, 1, -1, 100_000, -250_000, 999_999_999])
def test_rate_one_is_exact_no_op(n: int) -> None:
    # The INR backward-compat guarantee: rate == 1 returns the input unchanged (incl. sign/0).
    assert to_inr_paise(n, Decimal("1")) == n


def test_pinned_conversion() -> None:
    # $1000.00 (100000 cents) at ₹83.25 → ₹83,250.00 (8_325_000 paise).
    assert to_inr_paise(100_000, Decimal("83.25")) == 8_325_000


def test_sign_preserved() -> None:
    assert to_inr_paise(-100_000, Decimal("83")) == -8_300_000


def test_half_even_rounding() -> None:
    # 1 cent × 0.5 = 0.5 → rounds to even (0). 3 × 0.5 = 1.5 → rounds to even (2).
    assert to_inr_paise(1, Decimal("0.5")) == 0
    assert to_inr_paise(3, Decimal("0.5")) == 2
