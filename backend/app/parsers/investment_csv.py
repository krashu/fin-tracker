"""Canonical investment-transaction CSV parser (PRD §F7).

Pure: ``bytes`` → ``list[ParsedInvestmentRow]`` + per-row skip warnings. No DB, no
logging (a broker export may carry folio / PAN / holder name in stray columns — the
caller keeps it out of structured logs, and warnings here are line-number + reason
only, never raw cell contents).

**No per-broker parser.** One canonical field set; a repo-tracked :data:`HEADER_ALIASES`
maps each canonical field to the header spellings brokers actually use (seeded with the
Zerodha Console *Tradebook*), so the raw export usually imports with no renaming.
Unknown / appended columns are ignored. When a broker renames a column (rare) add one
spelling to :data:`HEADER_ALIASES` — an isolated edit, no code restructure.

**Money discipline.** ``price`` / ``amount`` / ``units`` / ``fees`` are parsed via
``Decimal`` (never ``float``) and converted to integer paise at the boundary. ``amount``
is authoritative when present, else derived ``units * price``. A row carries INR (the
default when the ``currency`` column is absent) or USD — the investment side's two
currencies; any other currency is skipped with a warning (the rest of the file still
imports), and a US-class row (``us_equity``/``us_etf``) resolving to INR is skipped (Yahoo
prices it in USD, so an INR stamp would value it 1:1 — the cent↔paise bug).

**Identity.** ``symbol`` is the instrument key — the broker ticker, normalised
(``strip().upper()``). Dedup keys on the resolved ``instrument_id``, not this string —
see ``investment_import_service._fingerprint``. ``isin`` (when present) is also captured
and stored on the instrument (fill-if-null); it keys the NAV/price match (AMFI NAVAll for
MFs, equity quotes) but never affects identity or dedup.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date as date_t
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import cast, get_args

from app.models.account import CurrencyStr
from app.models.instrument import AssetClassStr, ExchangeStr
from app.models.investment_transaction import InvestmentTxnTypeStr
from app.parsers.base import ParserError

# Canonical field → accepted source header spellings (lowercased). Repo-tracked and
# meant to be edited: add a broker's native spelling here to accept its raw export.
# Spellings must be DISJOINT across fields (a test asserts no spelling maps to two
# fields). ``isin`` is captured and stored on the instrument (fill-if-null) — it keys the
# NAV/price match but is not part of dedup identity.
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "trade_date", "transaction_date"),
    "type": ("type", "trade_type", "transaction_type"),
    "symbol": ("symbol", "tradingsymbol", "scrip", "ticker"),
    "isin": ("isin",),
    "name": ("name", "instrument_name", "scrip_name"),
    "asset_class": ("asset_class", "assetclass"),
    "exchange": ("exchange",),
    "units": ("units", "quantity", "qty"),
    "price": ("price", "average_price", "price_per_unit"),
    "amount": ("amount", "value", "net_amount"),
    "fees": ("fees", "charges"),
    "currency": ("currency",),
}

_REQUIRED_FIELDS = frozenset({"date", "type", "symbol", "units", "price"})
# Reverse index: alias spelling → canonical field. Built once at import.
_ALIAS_TO_FIELD: dict[str, str] = {
    spelling: field for field, spellings in HEADER_ALIASES.items() for spelling in spellings
}

_ASSET_CLASSES = frozenset(get_args(AssetClassStr))
# US-priced classes (Yahoo, native USD) — an INR currency on these is a mis-pricing
# contradiction, not a valid INR holding, so the row is skipped (see ``_parse_row``).
_US_ASSET_CLASSES = frozenset({"us_equity", "us_etf"})
_TXN_TYPES = frozenset(get_args(InvestmentTxnTypeStr))
# In the vocabulary but never importable via CSV (CAS-era / corporate-action history).
# Mirrors the manual-entry rejection in InvestmentTransactionCreate.
_CSV_DISALLOWED_TYPES = frozenset({"split", "switch_in", "switch_out"})
# Case-insensitive exchange lookup that preserves canonical casing ("MFCentral").
_EXCHANGE_BY_LOWER: dict[str, str] = {e.lower(): e for e in get_args(ExchangeStr)}
_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d")


@dataclass(frozen=True, slots=True)
class ParsedInvestmentRow:
    """One canonical CSV row → one ``investment_transaction`` (unsigned magnitudes).

    ``line_no`` is the 1-based source row (header = 1) — carried so the import
    service's per-type validation can warn by line number, never by cell contents.
    """

    line_no: int
    symbol: str
    isin: str | None
    name: str
    asset_class: AssetClassStr
    exchange: ExchangeStr
    currency: CurrencyStr
    date: date_t
    txn_type: InvestmentTxnTypeStr
    units: Decimal
    price: Decimal | None
    amount_native_paise: int
    fees_native_paise: int


class CSVParseError(ParserError):
    """File-level CSV failure: bad encoding, missing/duplicate required column, or zero
    importable rows. Maps to HTTP 422 at the route."""


def _to_paise(amount: Decimal) -> int:
    return int((amount * 100).to_integral_value(rounding=ROUND_HALF_EVEN))


def _q8(value: Decimal) -> Decimal:
    """Quantize to 8 dp — the scaled-int storage precision — at the boundary."""
    return value.quantize(Decimal("1e-8"), rounding=ROUND_HALF_EVEN)


def _to_decimal(text: str) -> Decimal:
    """Parse a numeric cell (commas/spaces stripped). Raises ``InvalidOperation``."""
    return Decimal(text.replace(",", "").replace(" ", ""))


def _try_date(text: str | None) -> date_t | None:
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_investment_csv(
    file_bytes: bytes, *, default_asset_class: AssetClassStr
) -> tuple[list[ParsedInvestmentRow], list[str]]:
    """Parse a canonical investment CSV into rows + per-row skip warnings.

    ``default_asset_class`` is applied to rows without an ``asset_class`` column/value
    (e.g. a single-asset Zerodha tradebook, where the user picks the class once in the
    upload form). Per-type magnitude validation (buy needs units+price, etc.) is the
    import service's boundary; this parser does structural + vocabulary + currency checks.

    Raises:
        CSVParseError: bad encoding, missing/duplicate required column, or zero
            importable rows.
    """
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise CSVParseError("file is not valid UTF-8 text") from e

    # Tolerate leading blank lines, then the first row is the header.
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    reader = csv.DictReader(io.StringIO("\n".join(lines)))

    col_map: dict[str, str] = {}
    for header in reader.fieldnames or []:
        if header is None:
            continue
        field = _ALIAS_TO_FIELD.get(header.strip().lower())
        if field is None:
            continue  # unknown / appended column — ignored
        if field in col_map:
            raise CSVParseError(f"duplicate column for {field!r}")
        col_map[field] = header

    missing = _REQUIRED_FIELDS - col_map.keys()
    if missing:
        raise CSVParseError(f"missing required column(s): {', '.join(sorted(missing))}")

    rows: list[ParsedInvestmentRow] = []
    warnings: list[str] = []
    for line_no, raw in enumerate(reader, start=2):  # row 1 is the header
        values = {
            field: ((raw.get(header) or "").strip() or None) for field, header in col_map.items()
        }
        result = _parse_row(values, line_no=line_no, default_asset_class=default_asset_class)
        if isinstance(result, str):
            warnings.append(result)
        else:
            rows.append(result)

    if not rows:
        raise CSVParseError("no valid investment transactions found")
    return rows, warnings


def _parse_row(
    values: dict[str, str | None], *, line_no: int, default_asset_class: AssetClassStr
) -> ParsedInvestmentRow | str:
    """Build one row, or return a PII-safe skip reason (line number + cause only).

    Currency resolves to INR (the default when the ``currency`` column is absent) or USD;
    any other currency is a per-row skip. A US-class row (``us_equity``/``us_etf``) resolving
    to INR is also skipped — Yahoo prices it in USD, so an INR stamp would value it 1:1.
    """
    raw_symbol = values.get("symbol")
    if not raw_symbol:
        return f"row {line_no}: missing symbol"
    symbol = raw_symbol.upper()
    isin = (values.get("isin") or "").upper() or None

    currency = (values.get("currency") or "INR").upper()
    if currency not in ("INR", "USD"):
        return f"row {line_no}: unsupported currency {currency} (INR/USD only)"

    type_raw = values.get("type")
    if not type_raw:
        return f"row {line_no}: missing transaction type"
    txn_type = type_raw.lower()
    if txn_type not in _TXN_TYPES or txn_type in _CSV_DISALLOWED_TYPES:
        return f"row {line_no}: unsupported transaction type"

    txn_date = _try_date(values.get("date"))
    if txn_date is None:
        return f"row {line_no}: invalid or missing date"

    units_raw = values.get("units")
    price_raw = values.get("price")
    amount_raw = values.get("amount")
    fees_raw = values.get("fees")
    try:
        units = _q8(_to_decimal(units_raw)) if units_raw else Decimal("0")
        price = _q8(_to_decimal(price_raw)) if price_raw else None
        fees_paise = _to_paise(_to_decimal(fees_raw)) if fees_raw else 0
        if amount_raw:
            amount_paise = _to_paise(_to_decimal(amount_raw))
        elif price is not None and units > 0:
            amount_paise = _to_paise(units * price)
        else:
            amount_paise = 0
    except InvalidOperation:
        return f"row {line_no}: invalid number"

    exchange = _EXCHANGE_BY_LOWER.get((values.get("exchange") or "OTHER").lower())
    if exchange is None:
        return f"row {line_no}: unknown exchange"

    asset_class = (values.get("asset_class") or default_asset_class).lower()
    if asset_class not in _ASSET_CLASSES:
        return f"row {line_no}: unknown asset_class"
    if asset_class in _US_ASSET_CLASSES and currency != "USD":
        return f"row {line_no}: {asset_class} requires currency=USD"

    return ParsedInvestmentRow(
        line_no=line_no,
        symbol=symbol,
        isin=isin,
        name=values.get("name") or symbol,
        asset_class=cast("AssetClassStr", asset_class),
        exchange=cast("ExchangeStr", exchange),
        currency=currency,
        date=txn_date,
        txn_type=cast("InvestmentTxnTypeStr", txn_type),
        units=units,
        price=price,
        amount_native_paise=amount_paise,
        fees_native_paise=fees_paise,
    )
