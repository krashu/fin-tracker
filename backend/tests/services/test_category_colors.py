"""The category palette and the root-colour assignment rule (ADR-0012 rule 7)."""

from __future__ import annotations

import math

from app.services.category_colors import CATEGORY_PALETTE, next_root_color, sibling_series_color

P = CATEGORY_PALETTE


def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _oklab(hex_: str) -> tuple[float, float, float]:
    r, g, b = (_lin(int(hex_[i : i + 2], 16) / 255) for i in (1, 3, 5))
    l_ = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m_ = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s_ = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = l_ ** (1 / 3), m_ ** (1 / 3), s_ ** (1 / 3)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def test_palette_neighbours_clear_the_normal_vision_floor() -> None:
    """Neighbouring slots, the last and first included, are at least deltaE 15 apart."""
    n = len(P)
    assert n == 14
    for i in range(n):
        delta = 100 * math.dist(_oklab(P[i]), _oklab(P[(i + 1) % n]))
        assert delta >= 15, (P[i], P[(i + 1) % n], delta)


def test_palette_sits_in_the_dark_band_inside_the_light_band_above_the_chroma_floor() -> None:
    for hex_ in P:
        lightness, a, b = _oklab(hex_)
        assert 0.48 <= lightness <= 0.67, (hex_, lightness)
        assert math.hypot(a, b) >= 0.10, (hex_, math.hypot(a, b))


def test_next_root_color_takes_the_least_used_slot_earliest_first() -> None:
    assert next_root_color([P[0], P[1], None, "#94a3b8"]) == P[2]
    assert next_root_color(list(P)) == P[0]
    assert next_root_color([*P, P[0], P[1]]) == P[2]


def test_inheriting_siblings_take_the_palette_slots_after_their_parents() -> None:
    assert [sibling_series_color(P[6], k) for k in range(13)] == [*P[7:], *P[:6]]
    assert sibling_series_color(P[6], 13) == P[7]
    assert P[6] not in {sibling_series_color(P[6], k) for k in range(40)}
    assert [sibling_series_color("#94a3b8", k) for k in range(3)] == list(P[:3])
