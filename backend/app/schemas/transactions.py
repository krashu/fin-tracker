"""Transaction read, create, and partial-update schemas (PRD §F1 read path, §F2 create, PATCH).

Flat shape: FK ids only, no denormalised joins. The field set is
deliberately tight — ``fingerprint`` stays internal (PRD §F4 dedup key),
and ``source`` / ``import_batch_id`` have no UI consumer. ``transfer_pair_id``
is on :class:`TransactionRead` for the F4a "Linked CC bill payment" banner
(ADR-0002 pairing).
"""

from __future__ import annotations

from datetime import date as date_t
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.transaction import TransactionTypeStr
from app.schemas.labels import LabelRead
from app.services.bulk_categorise import BULK_MAX
from app.services.merchant import merchant_too_long

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

    A merchant that normalizes past the 512-wide column is refused here (ADR-0006 rule 3b).
    """
    if v is None:
        return None
    v = v.strip() or None
    if v is not None and merchant_too_long(v):
        raise ValueError("merchant is longer than 512 characters once normalized")
    return v


def sign_error(transaction_type: TransactionTypeStr, amount_paise: int) -> str | None:
    """The sign rule (ADR-0009 rule 3), or ``None`` when the pair is valid.

    Every type accepts any non-zero sign; zero is rejected for all of them. The sign
    carries meaning rather than validity: a **positive spend is a refund** and a
    **negative income is an income reversal** (ADR-0009 rule 2), each netting its
    category down in the signed-sum aggregates. The accepted cost: a fat-fingered
    sign reads as a refund or a reversal instead of 422-ing. ``transaction_type`` is
    still a parameter so the rule stays one helper with one call shape.

    Returns a message rather than raising so both callers can raise in their own
    idiom — :meth:`TransactionCreate._check_sign` as a ``ValueError`` pydantic turns
    into a 422 body, and ``PATCH /transactions`` as an ``HTTPException``. The route
    needs it because ADR-0006 rule 12 validates the *merged* state: a PATCH may send
    a new type, a new amount, or only one of the two, and a schema validator cannot
    see the stored row.
    """
    if amount_paise == 0:
        return "amount_paise must be non-zero"
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
    # Denormalised display names for `category_id`, resolved WITHOUT the
    # `archived_at` filter (`category_service.resolve_category_labels`). Archiving is
    # soft (ADR-0012) so this row keeps its `category_id`, but `GET /categories`
    # serves active rows only — leaving the frontend unable to name an archived
    # category, which it rendered as "Uncategorized". That contradicted the archive
    # dialog's own promise and read as "your category assignment was lost".
    # `parent_name` carries the "Parent → Child" breadcrumb for a subcategory; both
    # are None when `category_id` is None, and `category_parent_name` is None for a
    # root category. Same contract as `SpendByCategoryRow.category_name`.
    category_name: str | None = None
    category_parent_name: str | None = None
    # ADR-0002 pairing, surfaced for the F4a "Linked CC bill payment" banner and its
    # break-link control (PRD §F4a-1) — the consumer the module docstring above
    # anticipated. Non-null also means the row's identity columns and type are frozen
    # until it is unlinked (ADR-0006 rule 15), so the dialog needs it to explain that
    # on screen rather than letting the user discover it as a 422. Ships in the same
    # commit as that renderer: a wire field with no renderer is exactly the drift
    # frontend/AGENTS.md documents (tsc cannot see a backend field the TS type never
    # declares, which is how fx_unavailable_count was computed and discarded).
    transfer_pair_id: int | None
    # ADR-0009 rule 13: the confirmed spend this refund reverses, or None. Informational,
    # never summed; rendered by the dialog's refund picker and shipped with it, per the
    # house rule above.
    refund_of_id: int | None
    # ADR-0002 rule 11b: server-computed placement of the manual card-bill link ("Paid your
    # card bill elsewhere?"): a confirmed, unpaired, positive `income` or `transfer` row on a
    # live card. Rendered by the transaction dialog and shipped with it, per the house rule
    # above; the client never re-derives it. Required, so every response is built by
    # `services.transaction_reads.transaction_reads`, never by validating an ORM row directly.
    card_bill_link_offered: bool
    # F3a labels — the row's user tags. list[LabelRead] (not a scalar FK) is a
    # deliberate deviation from the flat-FK-ids rule: an M2M has no single id.
    # Read via the viewonly Transaction.labels relationship (selectinload in the
    # list paths to avoid N+1).
    labels: list[LabelRead] = Field(default_factory=list)


class TransactionCreate(BaseModel):
    """Create body for ``POST /api/v1/transactions`` (PRD §F2 manual entry).

    Sign rule (:func:`sign_error`, ADR-0009 rule 3): every type accepts any
    non-zero amount; zero is rejected for all types. A **positive spend is a
    refund** and a **negative income is an income reversal** — the UI offers
    four entry directions and maps each to (type, sign) client-side (ADR-0009
    rule 12). The consequence is that a mistyped sign is indistinguishable from a
    deliberate refund or reversal at this boundary; that trade is recorded in
    ADR-0009.

    ``merchant_raw`` is optional (a manual row may legitimately have no
    merchant). It's stripped of leading/trailing whitespace; pure whitespace
    (``"   "``) and ``""`` both resolve to ``None`` so a blank input persists
    SQL ``NULL``, not ``""``. The 256 cap is the API's own: the
    ``merchant_raw`` and ``merchant_normalized`` columns are ``String(512)``
    (migration 0020), and normalizing can lengthen text, so a merchant that
    normalizes past 512 is refused too (ADR-0006 rule 3b).

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


class RefundLinkSet(BaseModel):
    """Body for ``PUT /api/v1/transactions/{id}/refund-link`` (ADR-0009 rule 13).

    Points a refund (a positive ``spend``) at the confirmed negative ``spend`` it
    reverses. Its own endpoint rather than a PATCH key: PATCH takes the seven
    user-visible fields only (ADR-0006 rule 10).
    """

    model_config = ConfigDict(extra="forbid")

    original_id: int = Field(gt=0)


class TransactionLinkSet(BaseModel):
    """Body for ``POST /api/v1/transactions/{id}/link`` (ADR-0002 rule 11b).

    Names the other leg of the pair. Its own endpoint rather than a PATCH key: a link moves
    the type, the category and the pair together, and PATCH writes no pair column.
    """

    model_config = ConfigDict(extra="forbid")

    partner_id: int = Field(gt=0)


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
      confirmed and has not pinned (ADR-0011 rule 11) — which reads ``"seeded"``
      regardless of the threshold. ``"none"`` means the pair is *absent* entirely (no
      rule at all); collapsing that with a present-at-zero seed is exactly
      the distinction ``prefetch_tag_strength`` exists to preserve.
    * ``pinned`` — whether any ``merchant_tag_map`` row behind this
      ``(canonical, category)`` is user-authored (``pinned=True``). The prefill
      path is pinned-aware, so a freshly-pinned rule prefills at ``hit_count=1``;
      surfacing ``pinned`` here lets the picker render an "authored" state that
      outranks the low ``prior_matches`` confidence tint. ``False`` when the pair
      is absent. A pinned seed row (``hit_count`` stays 0, because ``pin_tag``
      never bumps it) reads ``confidence == "none"`` with ``pinned == True`` —
      never ``"seeded"``.
    * ``cc_payment_candidate`` — the row names a card-bill payment, so F4a-1 may auto-link
      it to its other leg at commit. On a card batch that is a positive ``income`` credit (the
      leg links to a bank debit); on a bank batch it is a debit (the leg links to the card's
      payment credit). Does **not** assert the link will happen — the parent account and a
      matching row are checked at commit (``auto_link_cc_bill``).
    * ``uncategorised_commits_as_transfer`` — this row, committed with no category, commits as
      an unpaired ``transfer`` (ADR-0002 rules 7b, 7c): a flagged bank debit on a bank a live
      card names "Paid from", dated inside a committed statement of that card, or a flagged
      card payment credit, always. It ignores the row's current category (a categorised row
      commits in its category's kind). ``False`` on every other row, so the review queue
      never derives it.

    Why a separate schema rather than widening :class:`TransactionRead`:
    these fields are review-only. The board endpoint deliberately omits
    them per the locked decision "no `confirmed_at` on TransactionRead"
    and its docstring rule "wire schema is intentionally narrow".
    """

    prior_matches: int = Field(ge=0)
    confidence: ConfidenceStr
    pinned: bool
    cc_payment_candidate: bool
    uncategorised_commits_as_transfer: bool


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
    ``merchant_normalized``, ``origin_fingerprint`` and its ``origin_*`` inputs, ``source``,
    ``import_batch_id``, ``confirmed_at``, ``auto_category_id`` and
    ``transfer_pair_id``. The line is a principle, not convenience: this body
    carries **what the user asserts about the money**, and stops at **what the
    system asserts about the row**.

    ``transaction_type`` accepts the full column vocabulary so the schema mirrors
    the column, and the route accepts ``"transfer"`` as a target on an unpaired row
    (ADR-0002 rule 7d; it writes no pair column) but rejects identity/type edits on a row
    that is currently paired.

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


class BulkCategorise(BaseModel):
    """``POST /transactions/bulk-categorise`` body (ADR-0004 rule 4)."""

    model_config = ConfigDict(extra="forbid")

    transaction_ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1, max_length=BULK_MAX)
    category_id: int = Field(gt=0)


class BulkCategoriseResult(BaseModel):
    updated: int  # rows whose category changed
    taught: int  # distinct canonical merchants taught


class BulkDelete(BaseModel):
    """``POST /transactions/bulk-delete`` body: all-or-nothing, at most ``BULK_MAX`` ids."""

    model_config = ConfigDict(extra="forbid")

    transaction_ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1, max_length=BULK_MAX)


class PastRowsRead(BaseModel):
    """``GET /transactions/{id}/past-rows``: the "apply to N recent <merchant> rows?" offer."""

    merchant: str  # the canonical merchant, for the prompt text
    category_id: int | None  # the source row's category, to send back to bulk-categorise
    transaction_ids: list[int]
