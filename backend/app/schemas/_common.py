"""Validation rules shared across more than one PATCH-body schema.

Private to :mod:`app.schemas` (not re-exported from ``__init__``) — these are
boundary-validation details, not wire types.
"""

from __future__ import annotations


def reject_null_name(value: str | None) -> str:
    """Reject an explicit ``null`` for a ``name`` backed by a NOT NULL column.

    Every PATCH body declares ``name`` as ``str | None`` so that *omitted* is
    expressible under the route's ``model_dump(exclude_unset=True)`` — which means
    Pydantic's type system does **not** reject an explicit ``null``, and each schema
    has to. Without this, ``null`` reaches ``setattr(obj, "name", None)``, the commit
    raises ``IntegrityError``, the per-constraint 409 matchers don't recognise it, and
    the catch-all handler returns a **500** where a 422 belongs.

    That is not hypothetical: it was live on ``AccountUpdate`` and
    ``InstrumentUpdate`` while ``CategoryUpdate`` and ``LabelUpdate`` enforced it —
    one rule, four copies, the fix on two of them. Hence one home.

    The message is load-bearing: existing tests pin ``"name cannot be cleared"``.
    """
    if value is None:
        raise ValueError("name cannot be cleared")
    return value
