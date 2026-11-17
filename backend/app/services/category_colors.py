"""The category colour palette and the root-colour assignment rule (ADR-0012 rule 7).

``CATEGORY_PALETTE`` is the fixed set of hexes the app hands out. The order is
load-bearing: a root created without a colour takes the slot held by the fewest
active roots of its kind (the earliest slot on a tie), and a chart that colours
its series in this order puts palette neighbours side by side. Every neighbouring
pair, the last and first included, clears the dataviz normal-vision floor on both
the light (``#ffffff``) and dark (``#171c22``) chart surfaces, so one hex serves
both themes.

Re-validate after any change to the palette (slot 0 is appended so the
last-to-first pair is tested)::

    node validate_palette.js "<the 14 hexes>,#2a78d6" --mode light --surface "#ffffff"
    node validate_palette.js "<the 14 hexes>,#2a78d6" --mode dark --surface "#171c22"

Figures at the time of writing: the worst neighbour normal-vision deltaE is 16.4
(teal to blue) and the worst CVD figure 9.3; aqua is 2.82:1 on ``#ffffff``, which
the labels and table views relieve.

See :doc:`/docs/adr/0012-category-hierarchy` rule 7.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Category, CategoryKindStr

CATEGORY_PALETTE: tuple[str, ...] = (
    "#2a78d6",  # blue
    "#008300",  # green
    "#d55181",  # magenta
    "#c98500",  # amber
    "#1baf7a",  # aqua
    "#d95926",  # orange
    "#6c5cd6",  # violet
    "#e34948",  # red
    "#0e97c4",  # cyan
    "#c23b6b",  # raspberry
    "#6f9e15",  # lime
    "#b246c0",  # purple
    "#a9682f",  # brown
    "#0e9488",  # teal
)


def next_root_color(used: Iterable[str | None]) -> str:
    """The palette slot held by the fewest of ``used``, the earliest slot on a tie.

    Off-palette colours (a user-picked hex) and ``None`` are ignored, so a full
    palette keeps spreading instead of handing out the same slot every time.
    """
    counts = Counter(c for c in used if c in CATEGORY_PALETTE)
    return min(CATEGORY_PALETTE, key=counts.__getitem__)


def active_root_colors(
    session: Session, *, user_id: UUID, kind: CategoryKindStr
) -> list[str | None]:
    """The colours of the user's active roots of ``kind``: what :func:`next_root_color` reads."""
    return list(
        session.scalars(
            select(Category.color).where(
                Category.user_id == user_id,
                Category.kind == kind,
                Category.parent_id.is_(None),
                Category.archived_at.is_(None),
            )
        )
    )


@dataclass(frozen=True, slots=True)
class CategoryColors:
    """What one category renders as: ``display`` everywhere, ``series`` in the trend drilldown."""

    display: str
    series: str


def sibling_series_color(parent_color: str, position: int) -> str:
    """The trend-drilldown series colour of the inheriting subcategory at ``position``.

    ``position`` is its 0-based place by id among its parent's current children. It takes the
    slot ``position + 1`` places after the parent's, cyclically over the other 13 slots, so
    the parent's own slot (kept by the drilldown's direct row) is never used. A parent colour
    that is not a palette slot counts from slot 0.
    """
    n = len(CATEGORY_PALETTE)
    if parent_color in CATEGORY_PALETTE:
        p = CATEGORY_PALETTE.index(parent_color)
        return CATEGORY_PALETTE[(p + 1 + position % (n - 1)) % n]
    return CATEGORY_PALETTE[position % n]


def _required(value: str | None, category_id: int) -> str:
    if value is None:
        raise RuntimeError(
            f"root category {category_id} has no color; ck_categories_root_has_color forbids this"
        )
    return value


def category_colors(session: Session, *, user_id: UUID) -> dict[int, CategoryColors]:
    """Every one of the user's categories (archived included) mapped to its colours.

    ``display`` is the category's own colour, else its parent's (ADR-0012 rule 7): what every
    list and comparison renders. ``series`` is ``display`` except for an inheriting
    subcategory, which takes :func:`sibling_series_color` by its position among its parent's
    children (explicit-colour and archived siblings keep their positions). The trend drilldown
    is the only user of ``series``.
    """
    rows = session.execute(
        select(Category.id, Category.parent_id, Category.color)
        .where(Category.user_id == user_id)
        .order_by(Category.id)
    ).all()
    # Built before the pass: after a reparent a child can have a lower id than its parent.
    own = {r.id: r.color for r in rows}
    seen: Counter[int] = Counter()
    out: dict[int, CategoryColors] = {}
    for r in rows:
        if r.parent_id is None:
            c = _required(r.color, r.id)
            out[r.id] = CategoryColors(c, c)
            continue
        if r.color is not None:
            out[r.id] = CategoryColors(r.color, r.color)
        else:
            pc = _required(own[r.parent_id], r.parent_id)
            out[r.id] = CategoryColors(pc, sibling_series_color(pc, seen[r.parent_id]))
        seen[r.parent_id] += 1
    return out
