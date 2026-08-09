"""Label request/response schemas (PRD §F3a — user tags).

The wire shape is deliberately tiny: a label is just a name (user-facing "Tag",
rendered with a leading ``#``). ``user_id`` is omitted (single-user → multi-user
v2 boundary, like ``CategoryRead``). Normalisation (strip ``#``, lowercase,
collapse whitespace, drop ``;``, cap 64) is **not** done here — it lives in
``services/transaction_labels.normalize_label_name``, the single source shared by
the labels router and the transaction label-assignment path. The schemas only
bound length so an absurd payload 422s early; ``extra="forbid"`` surfaces typos.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas._common import reject_null_name


class LabelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 64 matches Label.name; a leading "#" is stripped by normalize in the route,
    # so the pathological "#"+64-char case is the only input this rejects early.
    name: str = Field(min_length=1, max_length=64)


class LabelRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class LabelUpdate(BaseModel):
    """Partial-update body for ``PATCH /labels/{id}`` — rename only.

    ``name`` is omittable (empty-body PATCH is a no-op, mirroring
    ``CategoryUpdate``) but **not nullable**: ``Label.name`` is NOT NULL, so an
    explicit ``null`` is rejected 422 rather than setattr-ing ``None`` into a
    NOT NULL column.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("name", mode="after")
    @classmethod
    def _not_null(cls, v: str | None) -> str | None:
        return reject_null_name(v)
