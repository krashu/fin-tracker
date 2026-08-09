"""Category request/response schemas (PRD §F5).

``CategoryCreate`` / ``CategoryUpdate`` use ``extra="forbid"`` so typos
surface as 422 instead of silent no-ops. ``name`` is stripped and bounded
to match ``Category.name`` (``String(64)``); SQLite does not enforce
VARCHAR length, so the Pydantic gate is the only one until v2 Postgres.

``CategoryRead`` exposes ``is_seeded`` so the UI can render a "default"
badge, ``kind`` so pickers can show only spend or only income categories,
and ``archived_at`` so clients can filter without re-querying. ``user_id``
is intentionally omitted to keep the wire shape narrow (and to keep the
single-user → multi-user v2 boundary clean).

``kind`` (spend|income) is set at create and **immutable** thereafter —
see ``CategoryUpdate``.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import CategoryKindStr
from app.schemas._common import reject_null_name

# A category's color is an arbitrary ``#rrggbb`` hex (the frontend offers a
# curated palette plus a custom picker). Stored lowercase in the ``String(16)``
# column; NULL on the wire and in the DB means "derive the color from the id"
# (the Auto fallback in ``frontend/lib/categories.ts``). The 3-digit (#rgb) and
# 8-digit (#rrggbbaa) forms are intentionally rejected — one canonical shape.
_HEX_COLOR_RE = re.compile(r"^#[0-9a-f]{6}$")


def _normalize_color(value: str | None) -> str | None:
    """Lower-case + validate a ``#rrggbb`` hex; pass ``None`` (Auto) through."""
    if value is None:
        return None
    normalized = value.strip().lower()
    if not _HEX_COLOR_RE.match(normalized):
        raise ValueError("color must be a hex string like #4f46e5")
    return normalized


def _strip_name(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("name must not be blank")
    return stripped


class CategoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    # Defaults to spend so the existing {name}-only POST stays valid until the
    # frontend sends kind. Literal-bound → an invalid kind 422s.
    kind: CategoryKindStr = "spend"
    # Optional hex color; None (default) keeps the derive-from-id behaviour.
    color: str | None = None

    @field_validator("name", mode="after")
    @classmethod
    def _strip(cls, v: str) -> str:
        return _strip_name(v)

    @field_validator("color", mode="after")
    @classmethod
    def _color(cls, v: str | None) -> str | None:
        return _normalize_color(v)


class CategoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    kind: CategoryKindStr
    is_seeded: bool
    archived_at: datetime | None
    color: str | None


class CategoryUpdate(BaseModel):
    """Partial-update body for ``PATCH /categories/{id}``.

    ``name`` is *omittable* (empty-body PATCH is a no-op, mirroring
    ``TransactionUpdate``) but **not nullable**: the underlying
    ``Category.name`` column is NOT NULL, so an explicit ``null`` is
    rejected with 422 ``"name cannot be cleared"``. The validator
    distinguishes "omitted" (field not in ``model_fields_set``) from
    "explicit null" — Pydantic's ``min_length=1`` does NOT reject
    ``None`` because the type is ``str | None``. Without this guard the
    route would setattr ``name=None`` and crash with a 500 on the NOT
    NULL constraint.

    Seeded categories ARE renameable; ``is_seeded`` records origin, not
    the current name.

    ``kind`` is deliberately **not** a field here — it is immutable after
    create. Flipping a category's scope would orphan every transaction
    already tagged under the old scope (a spend row left pointing at an
    income category, breaking the spend/income invariant and the
    dashboard signed-sums). ``extra="forbid"`` already 422s an attempt to
    PATCH ``kind``; rename stays the only mutation.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=64)
    # Unlike ``name``, ``color`` IS nullable: an explicit ``null`` is the
    # documented way to clear a picked color and revert to derive-from-id.
    # Omitted (not in model_fields_set) is a no-op via exclude_unset in the route.
    color: str | None = None

    @field_validator("name", mode="after")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return _strip_name(reject_null_name(v))

    @field_validator("color", mode="after")
    @classmethod
    def _color(cls, v: str | None) -> str | None:
        # Explicit null is allowed here (reverts to Auto), unlike name.
        return _normalize_color(v)
