"""Investment request/response schemas (PRD §F7).

Three concerns: ``Instrument`` CRUD, ``InvestmentTransaction`` create/read/update,
and the computed ``Holding`` read-model.

**Decimal-as-string on the wire.** ``units`` / prices / NAV / fx-rate are exact
``Decimal``s (stored as scaled ints — see :mod:`app.models.types`). They serialize
to JSON **strings**, not floats, so the precision survives the round-trip to a
JavaScript client (whose ``number`` is IEEE-754). Pydantic still accepts a JSON
string *or* number on input and parses it losslessly to ``Decimal``. Money stays
integer paise (``*_native_paise``) exactly as the spend side does.

**Currency.** Instruments accept INR + USD (the investment side's two currencies — PRD
§Non-goals). ``InvestmentTransactionCreate.fx_rate_to_inr`` is server-stamped at the route
from the cached FX rate (see ``investment_transactions`` route), not client-supplied.

**Per-type rules** (PRD §F7 lines 227-251) are enforced here at the HTTP boundary,
not in the DB: buy/sip/sell need units+price+amount; ``dividend`` is a cash payout
(``units == 0``, no price); ``bonus`` is free units (no price, no cashflow).
``split`` / ``switch_*`` are CAS-era and rejected on the manual-entry path.
"""

from __future__ import annotations

from datetime import date as date_t
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator, model_validator

from app.models.account import CurrencyStr
from app.models.instrument import AssetClassStr, ExchangeStr
from app.models.investment_transaction import InvestmentTxnTypeStr
from app.schemas._common import reject_null_name

# A Decimal that serializes to a fixed-point JSON string (never scientific
# notation, never a lossy float). ``normalize()`` strips quantize padding (e.g.
# avg-cost "100.00000000" → "100") so all decimal fields read uniformly; the
# ``"f"`` format keeps normalize's ``1E+2`` form rendered as plain "100". Input
# still accepts string or number.
DecimalStr = Annotated[
    Decimal,
    PlainSerializer(lambda v: format(v.normalize(), "f"), return_type=str, when_used="json"),
]


def normalise_isin(v: object) -> object:
    """Strip + upper-case an ISIN, blank → ``None``. A ``mode="before"`` validator.

    Byte-for-byte the normalisation ``parsers/investment_csv.py`` applies, so a scheme
    registered through the form and the same scheme arriving in a CSV cannot end up as
    two spellings of one identity key — ``_apply_mf_nav`` matches the AMFI index on exact
    string equality, so a stray space is an unpriceable holding. Non-``str`` input passes
    through untouched for Pydantic to reject with its own message.

    Runs *before* the 12-char length constraint deliberately: a padded value should be
    accepted and trimmed, not rejected for being 13 characters.
    """
    return (v.strip().upper() or None) if isinstance(v, str) else v


# --------------------------------------------------------------------------- #
# Instruments
# --------------------------------------------------------------------------- #
class InstrumentCreate(BaseModel):
    """POST body for ``/api/v1/instruments`` (PRD §F7 instruments).

    ``currency`` defaults to INR; USD is accepted (the investment side supports INR + USD —
    PRD §Non-goals). ``us_equity`` / ``us_etf`` must be USD — they're priced in USD (Yahoo), so
    an INR stamp would value them 1:1 at rollup (the cent↔paise bug); this mirrors the CSV
    parser's mismatch guard. ``current_nav`` is optional — a fresh instrument may have no price
    yet. When one *is* supplied, ``nav_as_of`` says what date that price is **valid for**
    (defaulting to today) and the route resolves it into ``nav_updated_at``. It is a valuation
    date, not an entry date: type an FD's accrued value off a statement dated 90 days ago and
    the staleness flags must read 90, not 0. See :class:`app.models.instrument.Instrument` for
    the column's contract; a future date is rejected at the route, where the clock lives.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=256)
    asset_class: AssetClassStr
    currency: CurrencyStr = "INR"
    exchange: ExchangeStr
    current_nav: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=8)
    nav_as_of: date_t | None = None
    isin: str | None = Field(default=None, min_length=12, max_length=12)

    @field_validator("isin", mode="before")
    @classmethod
    def _norm_isin(cls, v: object) -> object:
        return normalise_isin(v)

    @field_validator("symbol", "name", mode="after")
    @classmethod
    def _strip(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("must not be blank or whitespace-only")
        return stripped

    @model_validator(mode="after")
    def _check_us_currency(self) -> InstrumentCreate:
        if self.asset_class in ("us_equity", "us_etf") and self.currency != "USD":
            raise ValueError("us_equity / us_etf instruments must have currency=USD")
        return self

    @model_validator(mode="after")
    def _check_nav_as_of(self) -> InstrumentCreate:
        if self.nav_as_of is not None and self.current_nav is None:
            raise ValueError("nav_as_of requires current_nav — it dates a price, not a row")
        return self


class InstrumentRead(BaseModel):
    """Wire shape for one instrument.

    ``isin`` is exposed because it is now client-settable and **write-once** — a form that
    cannot see whether one is already stored cannot tell the user why their edit is
    refused, and cannot show which holdings are unmatchable to AMFI NAVAll.
    ``amfi_code`` stays hidden: it is server-derived on first NAV match and has no client
    use.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    name: str
    asset_class: AssetClassStr
    currency: CurrencyStr
    exchange: ExchangeStr
    isin: str | None
    current_nav: DecimalStr | None
    nav_updated_at: datetime | None
    archived_at: datetime | None


class InstrumentUpdate(BaseModel):
    """Partial-update body for ``PATCH /instruments/{id}``.

    Deliberately narrow: only ``name``, ``current_nav`` / ``nav_as_of``, and a *first*
    ``isin`` are writable. ``symbol`` / ``asset_class`` / ``currency`` / ``exchange`` are
    locked at creation — changing ``currency`` would invalidate every historical
    ``fx_rate_to_inr`` stamp (same argument as ``AccountUpdate``). Use
    ``model_dump(exclude_unset=True)`` so an omitted field is left alone while an
    explicit ``null`` clears the nav.

    ``nav_as_of`` is **not a column** — the route resolves it into ``nav_updated_at``, so
    it must be popped out before any generic ``setattr`` over the dump. It only dates a
    price, so it requires ``current_nav`` in the same body and is rejected alongside an
    explicit ``current_nav: null`` (clearing the price clears its valuation date). Sending
    it with an *unchanged* ``current_nav`` is a real edit — correcting the date a price was
    valid for — so it deliberately bypasses the route's idempotency short-circuit.

    ``isin`` is **write-once**: absent → set it (the importer's fill-if-null rule), already
    set → 422 unless the value matches, delete-and-re-create to change it. It must not ride
    the generic ``setattr`` loop either, which would clobber a stored identity key. A 422
    rather than the importer's silence because the two paths differ in kind: the importer
    ingests machine-generated values in bulk, where one conflicting row must not abort five
    hundred, while a PATCH is one deliberate human act — and a silent no-op there leaves
    the user believing they have fixed the AMFI pricing dead-end when they have not. It
    also matches the locked-field convention two paragraphs up.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=256)
    current_nav: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=8)
    nav_as_of: date_t | None = None
    isin: str | None = Field(default=None, min_length=12, max_length=12)

    @field_validator("isin", mode="before")
    @classmethod
    def _norm_isin(cls, v: object) -> object:
        return normalise_isin(v)

    @field_validator("name", mode="after")
    @classmethod
    def _strip_name(cls, v: str | None) -> str | None:
        stripped = reject_null_name(v).strip()
        if not stripped:
            raise ValueError("must not be blank or whitespace-only")
        return stripped

    @model_validator(mode="after")
    def _check_nav_as_of(self) -> InstrumentUpdate:
        if "nav_as_of" in self.model_fields_set and self.current_nav is None:
            raise ValueError(
                "nav_as_of requires current_nav in the same body — it dates a price, "
                "and clearing the price clears its date"
            )
        return self


class NavRefreshSummary(BaseModel):
    """Response body of ``POST /api/v1/instruments/refresh-navs`` (PRD §F7 / §F9).

    Counts from one snapshot run + PII-safe warnings (instrument ids and public reference
    data — AMFI scheme names / ISINs, Yahoo tickers — never a merchant or amount).

    ``catalogue_staleness_days`` is the oldest valuation across every **active priced
    instrument**, exited positions included — a catalogue-hygiene number, deliberately
    *not* the same as ``PortfolioPerformance.nav_staleness_days``, which folds the same
    expression over the currently-held set. They can differ by hundreds of days for one
    user at one instant; the names now say which is which.

    Built from ``nav_snapshot_service.NavRefreshResult`` (``from_attributes``).
    """

    model_config = ConfigDict(from_attributes=True)

    mf_updated: int
    equity_updated: int
    unmatched: int
    fetch_errors: int
    stale_skipped: int
    skipped: int
    null_nav_count: int
    catalogue_staleness_days: int | None
    warnings: list[str]


# --------------------------------------------------------------------------- #
# Investment transactions
# --------------------------------------------------------------------------- #
class InvestmentTransactionCreate(BaseModel):
    """POST body for ``/api/v1/investment-transactions`` (PRD §F7 manual entry).

    ``units`` and the money fields are **unsigned magnitudes**; the type carries
    direction (see the model docstring). ``pair_id`` is absent — it is server-managed,
    and the only pair this API writes comes from the dedicated reinvestment route
    (``switch_*`` remains rejected here). ``fx_rate_to_inr`` is **not** a client field — the
    route server-stamps it from the cached FX rate for the instrument's currency
    (INR → 1, USD → ``rate_on(date)``); ``extra="forbid"`` rejects a body that sends it.
    """

    model_config = ConfigDict(extra="forbid")

    date: date_t
    instrument_id: int = Field(gt=0)
    transaction_type: InvestmentTxnTypeStr
    # decimal_places caps input precision at the boundary (over-precise input is
    # rejected, not silently rounded at bind); max_digits keeps the scaled int64
    # in range on the future Postgres path.
    units: Decimal = Field(default=Decimal("0"), ge=0, max_digits=18, decimal_places=8)
    price_per_unit_native: Decimal | None = Field(
        default=None, gt=0, max_digits=18, decimal_places=8
    )
    amount_native_paise: int = Field(default=0, ge=0)
    fees_native_paise: int = Field(default=0, ge=0)
    note: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def _check_by_type(self) -> InvestmentTransactionCreate:
        t = self.transaction_type
        if t in ("buy", "sip", "sell"):
            if self.units <= 0:
                raise ValueError(f"{t} requires units > 0")
            if self.price_per_unit_native is None or self.price_per_unit_native <= 0:
                raise ValueError(f"{t} requires price_per_unit_native > 0")
            if self.amount_native_paise <= 0:
                raise ValueError(f"{t} requires amount_native_paise > 0")
        elif t == "dividend":
            if self.units != 0:
                raise ValueError("dividend requires units == 0 (cash payout, not new units)")
            if self.price_per_unit_native is not None:
                raise ValueError("dividend must not set price_per_unit_native")
            if self.amount_native_paise <= 0:
                raise ValueError("dividend requires amount_native_paise > 0")
        elif t == "bonus":
            if self.units <= 0:
                raise ValueError("bonus requires units > 0")
            if self.price_per_unit_native is not None:
                raise ValueError("bonus must not set price_per_unit_native (free units)")
            if self.amount_native_paise != 0:
                raise ValueError("bonus requires amount_native_paise == 0 (no cashflow)")
        else:
            # split / switch_in / switch_out are CAS-era, not manual entry.
            raise ValueError(f"{t} is not supported via manual entry")
        return self


class InvestmentTransactionRead(BaseModel):
    """Wire shape for one investment transaction.

    ``pair_id`` IS exposed, deliberately diverging from ``TransactionRead``, which omits
    ``transfer_pair_id`` because it has no UI consumer. Here there is both a writer and a
    consumer: without it a flat ``GET`` renders a reinvestment as two unrelated same-date
    events, which is the silent-drift complaint the pair exists to fix. Read-only —
    ``Create`` / ``Update`` do not accept it, so the write surface stays server-managed.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    instrument_id: int
    date: date_t
    transaction_type: InvestmentTxnTypeStr
    units: DecimalStr
    price_per_unit_native: DecimalStr | None
    amount_native_paise: int
    fees_native_paise: int
    fx_rate_to_inr: DecimalStr
    note: str | None
    pair_id: int | None


class InvestmentTransactionUpdate(BaseModel):
    """Partial-update body for ``PATCH /investment-transactions/{id}``.

    ``note`` only. Editing units/amount/type would silently rewrite the FIFO
    history a holding's cost basis depends on — correct those via DELETE +
    re-create (the row is hard-deletable). Mirrors the narrow ``AccountUpdate``.
    """

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=1024)


class ReinvestmentCreate(BaseModel):
    """POST body for ``/api/v1/investment-transactions/reinvestment`` (PRD §F7).

    An Indian MF **IDCW dividend-reinvestment**: ``amount_native_paise`` of dividend
    became ``units`` at NAV ``price_per_unit_native`` on ``date``. Persisted as a
    ``dividend`` row (the income) linked to a ``buy`` row (the acquisition), because one
    row cannot carry both without conflating income with acquisition — and conflating
    them is what breaks FIFO holding periods. The ``buy`` leg opens its own lot with its
    own cost basis and acquisition date.

    Three deliberate omissions:

    * **No ``fees_native_paise``** — a reinvestment carries no brokerage.
      ``extra="forbid"`` makes a client that sends one fail loudly rather than silently
      capitalising a fee into the lot.
    * **One ``amount_native_paise``, stamped on both legs.** v1 pins full reinvestment.
      Real IDCW deducts §194K TDS and reinvests the net, so gross and reinvested amounts
      *can* differ — but there is no TDS concept in the codebase or PRD §F7, and
      splitting them is a separate feature, not a silent default.
    * **No ``units × price ≈ amount`` cross-check.** AMC statements round units to 3 dp,
      so strict equality would reject real data and a tolerance would be a config knob.
      ``amount_native_paise`` is authoritative for cost basis (matching ``_apply``'s
      ``amount + fees``); ``units`` / ``price`` are as-reported. Same stance as the CSV
      parser's "amount is authoritative when present".

    No ``model_validator``: every rule is a field constraint.
    """

    model_config = ConfigDict(extra="forbid")

    date: date_t
    instrument_id: int = Field(gt=0)
    amount_native_paise: int = Field(gt=0)
    units: Decimal = Field(gt=0, max_digits=18, decimal_places=8)
    price_per_unit_native: Decimal = Field(gt=0, max_digits=18, decimal_places=8)
    note: str | None = Field(default=None, max_length=1024)


class ReinvestmentRead(BaseModel):
    """The two legs of a recorded reinvestment, named rather than positional.

    Naming the legs means a client never has to infer which is which from
    ``transaction_type``, and it gets both ids — needed to render the linkage and to
    unwind the pair (corrections are DELETE + re-create). Mirrors ``TransferRead``.
    """

    model_config = ConfigDict(from_attributes=True)

    dividend: InvestmentTransactionRead
    buy: InvestmentTransactionRead


# --------------------------------------------------------------------------- #
# Holdings (computed read-model — no table)
# --------------------------------------------------------------------------- #
class HoldingRead(BaseModel):
    """One current position, computed by ``holdings_service.compute_holdings``.

    ``invested_native_paise`` is the remaining FIFO cost basis (acquisition cost
    of the units still held, fees included). ``current_value_native_paise`` and
    ``unrealized_pnl_native_paise`` are ``null`` when the instrument has no NAV yet.
    The ``*_native_paise`` fields are in the instrument's own currency (per-row display);
    the ``*_inr_paise`` fields are the home-currency rollup values (cost basis at each lot's
    historical rate, current value at the as-of rate). ``current_value_inr_paise`` /
    ``unrealized_pnl_inr_paise`` are additionally ``null`` for a USD holding with no cached FX
    rate. For an INR holding the INR fields equal their native counterparts. ``net_units`` /
    ``avg_cost_native`` / ``current_nav`` are exact decimals serialized as strings.

    ``nav_staleness_days`` is this row's valuation age in calendar days — ``null`` when the
    holding has no NAV. Server-computed rather than a raw ``nav_updated_at`` stamp so the
    client never does date arithmetic on a naive timestamp string (see
    ``holdings_service.compute_holdings``); the client's only job is to compare it against
    ``schemas.performance.STALENESS_WARN_DAYS``.
    """

    model_config = ConfigDict(from_attributes=True)

    instrument_id: int
    symbol: str
    name: str
    asset_class: AssetClassStr
    currency: CurrencyStr
    net_units: DecimalStr
    avg_cost_native: DecimalStr
    invested_native_paise: int
    current_nav: DecimalStr | None
    current_value_native_paise: int | None
    unrealized_pnl_native_paise: int | None
    invested_inr_paise: int
    current_value_inr_paise: int | None
    unrealized_pnl_inr_paise: int | None
    nav_staleness_days: int | None


class HoldingsResponse(BaseModel):
    """Envelope for ``GET /api/v1/holdings`` — current positions, symbol-sorted."""

    holdings: list[HoldingRead]


# --------------------------------------------------------------------------- #
# Portfolio summary (computed — XIRR + asset-class allocation; PRD §F8 view 6)
# --------------------------------------------------------------------------- #
class AssetClassAllocation(BaseModel):
    """One asset-class slice of the allocation donut (NAV-bearing holdings only).

    ``value_paise`` is the summed current value of that class; the client derives
    each slice's share as ``value_paise / Σ value_paise``.
    """

    asset_class: AssetClassStr
    value_paise: int


class HoldingXirr(BaseModel):
    """Per-holding money-weighted return, keyed by ``instrument_id`` for a
    client-side merge into the holdings table. ``xirr`` is an annualized fraction
    (0.12 = 12%), ``null`` when unsolvable (degenerate cashflows / no NAV).
    """

    instrument_id: int
    xirr: float | None


class PortfolioSummary(BaseModel):
    """Response of ``GET /api/v1/portfolio/summary`` (PRD §F8 view 6 / §F9).

    Value / invested / unrealized P&L and ``holdings_count`` cover the NAV-bearing
    set only (a null-NAV holding contributes ₹0, tallied in ``null_nav_count``).
    ``fx_unavailable_count`` tallies USD holdings that *are* priced but have no cached FX
    rate — excluded from the totals and the XIRR, surfaced so the number isn't silently short.
    ``xirr`` is the portfolio-wide annualized fraction, ``null`` when unsolvable. All money is
    INR paise (multi-currency rolled up to INR). See ``portfolio_service`` for caveats.
    """

    current_value_paise: int
    invested_paise: int
    unrealized_pnl_paise: int
    xirr: float | None
    holdings_count: int
    null_nav_count: int
    fx_unavailable_count: int
    allocations: list[AssetClassAllocation]
    holding_xirr: list[HoldingXirr]
