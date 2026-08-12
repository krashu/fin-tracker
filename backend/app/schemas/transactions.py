"""Transaction read, create, and partial-update schemas (PRD §F1 read path, §F2 create, PATCH).

Flat shape: FK ids only, no denormalised joins. The field set is
deliberately tight — ``fingerprint`` stays internal (PRD §F4 dedup key),
and ``source`` / ``import_batch_id`` / ``transfer_pair_id`` have no UI
consumer yet. ``transfer_pair_id`` will surface here when F4a CC-bill
auto-link ships and the frontend wants to render a "paired" badge —
the schema bump is non-breaking (additive nullable field).
"""

from __future__ import annotations

from datetime import date as date_t
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.transaction import TransactionTypeStr
from app.schemas.labels import LabelRead

ConfidenceStr = Literal["confident", "uncertain", "seeded", "none"]

# Per-txn label bounds. Item cap 64 matches Label.name / LabelCreate — an
# over-length name 422s at the boundary (consistent with POST /labels) instead of
# silently truncating in normalize_label_name. The list cap keeps a degenerate
# payload from building an oversized IN clause in resolve_label_names (SQLite
# bind-var limit → 500); 50 is far above any real transaction's tag count.
_LABEL_NAME_MAX = 64
MAX_LABELS_PER_TXN = 50
LabelName = Annotated[str, Field(max_length=_LABEL_NAME_MAX)]


def _stripped_or_none(v: str | None) -> str | None:
    """Trim a merchant; blank or pure whitespace becomes ``None`` (SQL NULL, not ``""``).

    Shared by ``TransactionCreate`` and ``TransactionUpdate`` so a merchant cleared on
    PATCH lands in the same state a blank one lands in on POST — which matters because
    the PRD §F4 fingerprint hashes ``normalize_merchant(raw) if raw else ""``, and a
    stored ``""`` would take a different branch than a stored NULL.
    """
    if v is None:
        return None
    return v.strip() or None


def sign_error(transaction_type: TransactionTypeStr, amount_paise: int) -> str | None:
    """The F2 sign rule, or ``None`` when the pair is valid.

    ``income > 0``; ``spend`` and ``transfer`` take any non-zero sign; zero
    rejected for all three. Wrong-sign rows would silently distort the F8
    signed-sum aggregates.

    ``spend`` is deliberately asymmetric with ``income``: a **positive spend is a
    refund** (ADR-0009), so the old ``spend < 0`` guard had to go. ``income``
    keeps ``> 0`` because an income reversal (a salary clawback) is out of scope
    for v1. The accepted cost: a fat-fingered positive spend now reads as a
    refund instead of 422-ing.

    Returns a message rather than raising so both callers can raise in their own
    idiom — :meth:`TransactionCreate._check_sign` as a ``ValueError`` pydantic turns
    into a 422 body, and ``PATCH /transactions`` as an ``HTTPException``. The route
    needs it because ADR-0007 rule 4 validates the *merged* state: a PATCH may send
    a new type, a new amount, or only one of the two, and a schema validator cannot
    see the stored row.
    """
    if amount_paise == 0:
        return "amount_paise must be non-zero"
    if transaction_type == "income" and amount_paise <= 0:
        return "income requires positive amount_paise"
    # spend / transfer: any non-zero sign accepted (zero already rejected above).
    # A positive spend IS the refund representation — see the docstring.
    return None


class TransactionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    date: date_t
    amount_paise: int
    transaction_type: TransactionTypeStr
    merchant_raw: str | None
    category_id: int | None
    # ADR-0002 pairing, surfaced for the F4a "Linked CC bill payment" banner and its
    # break-link control (PRD §F4a-1) — the consumer the module docstring above
    # anticipated. Non-null also means the row's identity columns and type are frozen
    # until it is unlinked (ADR-0007 rule 7), so the dialog needs it to explain that
    # on screen rather than letting the user discover it as a 422. Ships in the same
    # commit as that renderer: a wire field with no renderer is exactly the drift
    # frontend/CLAUDE.md documents (tsc cannot see a backend field the TS type never
    # declares, which is how fx_unavailable_count was computed and discarded).
    transfer_pair_id: int | None
    # F3a labels — the row's user tags. list[LabelRead] (not a scalar FK) is a
    # deliberate deviation from the flat-FK-ids rule: an M2M has no single id.
    # Read via the viewonly Transaction.labels relationship (selectinload in the
    # list paths to avoid N+1).
    labels: list[LabelRead] = Field(default_factory=list)


class TransactionCreate(BaseModel):
    """Create body for ``POST /api/v1/transactions`` (PRD §F2 manual entry).

    Sign rule: ``income > 0``; ``spend`` and ``transfer`` accept any non-zero
    amount. Zero rejected for all types. Wrong-sign rows would silently distort
    dashboard signed-sum aggregates.

    ``spend`` accepts both signs because a **positive spend is a refund**
    (ADR-0009) — the UI still offers a three-way Spend/Refund/Income choice and
    maps it to (type, sign) client-side. The consequence is that a mistyped
    positive spend is now indistinguishable from a deliberate refund at this
    boundary; that trade is recorded in ADR-0009.

    ``merchant_raw`` is optional (a manual row may legitimately have no
    merchant). It's stripped of leading/trailing whitespace; pure whitespace
    (``"   "``) and ``""`` both resolve to ``None`` so a blank input persists
    SQL ``NULL``, not ``""``. The 256 cap matches
    ``transactions.merchant_normalized String(256)`` so a long raw can't
    silently overflow the normalized column on Postgres v2.

    No-merchant collision: two no-merchant rows with the same
    date+amount+account still collide → 409. The PRD §F4 fingerprint hashes
    ``merchant_normalized``, which is ``""`` for both (``None`` raw →
    ``""`` normalized), so they share a fingerprint.

    Fields NOT in the body (server-managed): ``fingerprint`` (PRD §F4 dedup),
    ``source`` (always ``"manual"`` for this endpoint), ``import_batch_id``
    (always ``None``), ``transfer_pair_id`` (F4a CC-bill auto-link owns it).
    """

    model_config = ConfigDict(extra="forbid")

    date: date_t
    account_id: int = Field(gt=0)
    amount_paise: int
    transaction_type: TransactionTypeStr
    # Optional: a manual row may have no merchant. Blank/whitespace persists
    # NULL, not "" (see _strip_merchant + the class docstring).
    merchant_raw: str | None = Field(default=None, max_length=256)
    category_id: int | None = Field(default=None, gt=0)
    # F3a labels — names, get-or-created on save. Empty list = no labels. Bounded
    # per-item (64) + per-list (MAX_LABELS_PER_TXN) so an over-long name 422s
    # (matching POST /labels) and a degenerate list can't blow the IN-clause.
    labels: list[LabelName] = Field(default_factory=list, max_length=MAX_LABELS_PER_TXN)

    @field_validator("merchant_raw", mode="after")
    @classmethod
    def _strip_merchant(cls, v: str | None) -> str | None:
        return _stripped_or_none(v)

    @model_validator(mode="after")
    def _check_sign(self) -> TransactionCreate:
        error = sign_error(self.transaction_type, self.amount_paise)
        if error is not None:
            raise ValueError(error)
        return self


class TransactionTransferCreate(BaseModel):
    """Create body for ``POST /api/v1/transactions/transfer`` (PRD §F2 transfer).

    Unlike :class:`TransactionCreate` — which takes a *signed* amount and lets
    ``_check_sign`` accept any non-zero sign for a transfer — this takes a
    *positive magnitude* and the route derives both legs' signs (source =
    ``-amount_paise`` outflow, dest = ``+amount_paise`` inflow). Server-derived
    signs make it impossible for the two legs to disagree.

    Server-managed (absent from the body): ``transaction_type`` (always
    ``"transfer"``), ``transfer_pair_id`` (the route cross-links the legs),
    ``fingerprint`` (PRD §F4 dedup), ``source`` (``"manual"``),
    ``import_batch_id`` (``None``). Merchant labels are auto-generated from the
    account names — a *point-in-time snapshot*; renaming an account does NOT
    backfill historical transfer labels.
    """

    model_config = ConfigDict(extra="forbid")

    date: date_t
    source_account_id: int = Field(gt=0)
    dest_account_id: int = Field(gt=0)
    amount_paise: int = Field(gt=0)

    @model_validator(mode="after")
    def _check_distinct_accounts(self) -> TransactionTransferCreate:
        # Load-bearing: the no-self-pair CHECK can't catch this (distinct rows
        # have distinct ids), and the two legs' fingerprints differ by sign — so
        # without this guard two rows would silently persist on one account.
        if self.source_account_id == self.dest_account_id:
            raise ValueError("source and destination accounts must differ")
        return self


class TransferRead(BaseModel):
    """Response for ``POST /transactions/transfer`` — the two created legs.

    A named ``{source, dest}`` envelope (not a bare list): the structure names
    which leg is which without inferring from the sign. ``from_attributes`` so
    the nested ORM ``Transaction`` rows coerce into :class:`TransactionRead`.
    """

    model_config = ConfigDict(from_attributes=True)

    source: TransactionRead
    dest: TransactionRead


class TransactionCandidate(TransactionRead):
    """Per-row payload for ``GET /imports/{batch_id}/candidates``.

    Extends :class:`TransactionRead` with the four review-only fields the
    frontend needs to render the queue:

    * ``prior_matches`` — the merchant memory behind this row's suggestion:
      a dict lookup of ``(canonical, category_id)`` in
      :func:`app.services.tag_service.prefetch_tag_strength`, so it is the
      ``hit_count`` SUMMED across every raw descriptor the user's alias table
      folds onto that canonical (ADR-0011), not one row's count. ``0`` when
      there is no such pair — including when ``category_id IS NULL``
      (income/transfer or new-merchant spend), and when the category has
      since been ARCHIVED, since the aggregate inherits that filter.
    * ``confidence`` — derived from ``prior_matches`` at thresholds locked
      in the route module (≥3 confident, 1-2 uncertain), **except** when the
      resolved ``(canonical, category)`` pair is present in the merchant map
      at ``hit_count == 0`` — a dictionary entry this user has never
      confirmed (ADR-0011 decision 4) — which reads ``"seeded"`` regardless
      of the threshold. ``"none"`` means the pair is *absent* entirely (no
      rule at all); collapsing that with a present-at-zero seed is exactly
      the distinction ``prefetch_tag_strength`` exists to preserve.
    * ``pinned`` — whether any ``merchant_tag_map`` row behind this
      ``(canonical, category)`` is user-authored (``pinned=True``). The prefill
      path is pinned-aware, so a freshly-pinned rule prefills at ``hit_count=1``;
      surfacing ``pinned`` here lets the picker render an "authored" state that
      outranks the low ``prior_matches`` confidence tint. ``False`` when the pair
      is absent. Note it can be ``True`` alongside ``confidence == "seeded"``:
      pinning a seed row leaves ``hit_count`` at 0, because ``pin_tag`` never
      bumps it on an existing row.
    * ``cc_payment_candidate`` — the row is ``income`` and its merchant names
      a card-bill payment, so F4a-1 may auto-link it to a bank debit at
      commit. Does **not** assert the link will happen — the parent account
      and a matching debit are checked at commit (``auto_link_cc_bill``
      gates 4-6).

    Why a separate schema rather than widening :class:`TransactionRead`:
    these fields are review-only. The board endpoint deliberately omits
    them per the locked decision "no `confirmed_at` on TransactionRead"
    and its docstring rule "wire schema is intentionally narrow".
    """

    prior_matches: int = Field(ge=0)
    confidence: ConfidenceStr
    pinned: bool
    cc_payment_candidate: bool


class TransactionUpdate(BaseModel):
    """Partial-update body for ``PATCH /transactions/{id}``.

    Only fields present in the JSON body are written; omitted means "leave alone".
    Use ``model_dump(exclude_unset=True)`` in the route to honour this.

    ``extra="forbid"`` rejects unknown keys with 422 so a typo (``"label"``
    instead of ``"labels"``) surfaces immediately rather than silently
    no-op-ing.

    **Every user-visible column is editable** (ADR-0007). Explicit ``null`` clears
    the two columns that are genuinely nullable — ``category_id`` and
    ``merchant_raw`` — and 422s on the four that are not (see
    :meth:`_reject_explicit_nulls`), because "unset the date" has no meaning.

    Two cost classes, one endpoint (rule 2). ``transaction_type`` / ``category_id``
    / ``labels`` are free: the type is absent from the ADR-0006 hash payload, so
    none of them touches identity. ``date`` / ``amount_paise`` / ``merchant_raw`` /
    ``account_id`` are the identity tuple — the route recomputes
    ``merchant_normalized`` and ``fingerprint`` and resets ``occurrence`` to 0 when
    one of them **actually changes**, so a no-op PATCH stays a no-op, and a
    collision surfaces as 409.

    Server-managed, never accepted here (rule 1): ``fingerprint``, ``occurrence``,
    ``merchant_normalized``, ``origin_fingerprint``, ``source``,
    ``import_batch_id``, ``confirmed_at``, ``auto_category_id`` and
    ``transfer_pair_id``. The line is a principle, not convenience: this body
    carries **what the user asserts about the money**, and stops at **what the
    system asserts about the row**.

    ``transaction_type`` accepts the full column vocabulary so the schema mirrors
    the column, but the route rejects ``"transfer"`` as a *target* (rule 7 — pairs
    are born via ``POST /transactions/transfer``, and a lone leg minted here would
    violate ADR-0002's exactly-two-pairing invariant) and rejects identity/type
    edits on a row that is currently paired.

    ``category_id`` is pre-flighted against ``categories`` (same user, not archived,
    kind matching the **post-patch** ``transaction_type``) → 422 ``"category not
    found, archived, or wrong kind for this transaction"``, surfacing cross-user /
    archived / unknown / wrong-kind ids rather than a generic FK ``IntegrityError``
    → 500. The FK alone would not catch cross-user assignment (it only proves the
    row exists), so the pre-flight is load-bearing for v2 multi-user.

    Convention for future fields: every editable column lives here with a matching
    ``default=None`` (and an appropriate validator). Do NOT switch to
    ``exclude_none=True`` — that loses the "null clears" path.
    """

    model_config = ConfigDict(extra="forbid")

    # The ADR-0006 identity tuple. `| None` here means "absent from the body", NOT
    # "clearable" — _reject_explicit_nulls below turns a literal null into a 422,
    # since exclude_unset is what distinguishes omitted from sent and these four
    # columns are NOT NULL.
    date: date_t | None = None
    amount_paise: int | None = None
    account_id: int | None = Field(default=None, gt=0)
    transaction_type: TransactionTypeStr | None = None
    # Nullable on the model, so explicit null (or blank/whitespace, via
    # _strip_merchant) legitimately clears it to SQL NULL.
    merchant_raw: str | None = Field(default=None, max_length=256)
    # F3a labels — REPLACE-SET: key present → the txn's label set becomes exactly
    # this list (explicit null coerced to [] = clear); omitted → left unchanged
    # (via exclude_unset in the route). `| None` is load-bearing (null = clear vs
    # omitted = leave-alone). Names are get-or-created on save; bounded per-item
    # (64) + per-list (MAX_LABELS_PER_TXN) exactly like TransactionCreate.
    labels: list[LabelName] | None = Field(default=None, max_length=MAX_LABELS_PER_TXN)
    category_id: int | None = Field(default=None, gt=0)

    @field_validator("merchant_raw", mode="after")
    @classmethod
    def _strip_merchant(cls, v: str | None) -> str | None:
        return _stripped_or_none(v)

    @model_validator(mode="after")
    def _reject_explicit_nulls(self) -> TransactionUpdate:
        """422 on ``{"date": null}`` and friends — those four columns are NOT NULL.

        ``model_fields_set`` is what separates "sent as null" from "omitted"; the
        route's ``exclude_unset`` dump cannot tell them apart once they are both
        ``None``, and a silent no-op would read as a successful clear.
        """
        for name in ("date", "amount_paise", "account_id", "transaction_type"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self
