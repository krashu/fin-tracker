"""Canonical investment-transaction CSV and Excel parser (PRD §F7).

Pure: ``bytes`` → ``list[ParsedInvestmentRow]`` + per-row skip warnings. No DB, no
logging (a broker export may carry folio / PAN / holder name in stray columns — the
caller keeps it out of structured logs, and warnings here are line-number + reason
only, never raw cell contents).

**No per-broker parser.** One canonical field set; a repo-tracked :data:`HEADER_ALIASES`
maps each canonical field to the header spellings brokers actually use (seeded with the
Zerodha Console *Tradebook* and Groww Orders), so the raw export usually imports with no renaming.
Unknown / appended columns are ignored. When a broker renames a column (rare) add one
spelling to :data:`HEADER_ALIASES` — an isolated edit, no code restructure.

**Format Detection.** Automatically detects OpenXML Excel (``.xlsx``, magic bytes ``PK\\x03\\x04``)
versus plain-text CSV (UTF-8). Legacy binary ``.xls`` (OLE2 compound document) is refused with
an explicit, actionable error (:class:`UnsupportedFileFormatError`).

**Money discipline.** ``price`` / ``amount`` / ``units`` / ``fees`` are parsed via
``Decimal`` (never ``float``) and converted to integer paise at the boundary. ``amount``
is authoritative when present, else derived ``units * price``. A row carries INR (the
default when the ``currency`` column is absent) or USD — the investment side's two
currencies; any other currency is skipped with a warning (the rest of the file still
imports), and a US-class row (``us_equity``/``us_etf``) resolving to INR is skipped (Yahoo
prices it in USD, so an INR stamp would value it 1:1 — the cent↔paise bug), as is an
INR-only row (``indian_equity``/``indian_mf``/``nps``) resolving to USD.

**Identity.** ``symbol`` is the instrument key — the broker ticker, normalised
(``strip().upper()``). Dedup keys on the resolved ``instrument_id``, not this string —
see ``investment_import_service._fingerprint``. ``isin`` (when present) is also captured
and stored on the instrument (fill-if-null); it keys the NAV/price match (AMFI NAVAll for
MFs, equity quotes) but never affects identity or dedup.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date as date_t
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import cast, get_args

import openpyxl
import openpyxl.utils.datetime

from app.models.account import CurrencyStr
from app.models.instrument import AssetClassStr, ExchangeStr, class_currencies
from app.models.investment_transaction import InvestmentTxnTypeStr
from app.parsers.base import ParserError

# Canonical field → accepted source header spellings (lowercased, snake_case). Repo-tracked
# and meant to be edited: add a broker's native spelling here to accept its raw export.
# Spellings must be DISJOINT across fields (a test asserts no spelling maps to two
# fields). ``isin`` is captured and stored on the instrument (fill-if-null) — it keys the
# NAV/price match but is not part of dedup identity.
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "trade_date", "transaction_date", "order_date", "execution_date"),
    "type": (
        "type",
        "trade_type",
        "transaction_type",
        "order_type",
        "side",
        "action",
        "buy/sell",
    ),
    "symbol": (
        "symbol",
        "tradingsymbol",
        "scrip",
        "ticker",
        "stock_symbol",
        "scrip_code",
    ),
    "isin": ("isin", "isin_code"),
    "name": (
        "name",
        "instrument_name",
        "scrip_name",
        "stock_name",
        "company_name",
        "security_name",
        "scheme_name",
    ),
    "asset_class": ("asset_class", "assetclass"),
    "exchange": ("exchange",),
    "units": ("units", "quantity", "qty", "shares", "no_of_shares", "executed_qty"),
    "price": (
        "price",
        "average_price",
        "price_per_unit",
        "avg_price",
        "execution_price",
        "rate",
        "trade_price",
        "nav",
    ),
    "amount": ("amount", "value", "total_amount", "total_value", "trade_value", "gross_amount"),
    "fees": ("fees", "charges", "total_charges", "brokerage", "commission"),
    "currency": ("currency",),
}

_REQUIRED_FIELDS = frozenset({"date", "type", "symbol", "units", "price"})
# Reverse index: alias spelling → canonical field. Built once at import.
_ALIAS_TO_FIELD: dict[str, str] = {
    spelling: field for field, spellings in HEADER_ALIASES.items() for spelling in spellings
}

_ASSET_CLASSES = frozenset(get_args(AssetClassStr))
_TXN_TYPES = frozenset(get_args(InvestmentTxnTypeStr))
# In the vocabulary but never importable via CSV/Excel (CAS-era / corporate-action history).
# Mirrors the manual-entry rejection in InvestmentTransactionCreate.
_CSV_DISALLOWED_TYPES = frozenset({"split", "switch_in", "switch_out"})
# Vocabulary synonyms mapping broker export words to canonical InvestmentTxnTypeStr
_TXN_TYPE_SYNONYMS: dict[str, InvestmentTxnTypeStr] = {
    "buy": "buy",
    "b": "buy",
    "purchase": "buy",
    "sell": "sell",
    "s": "sell",
    "redemption": "sell",
    "sip": "sip",
    "dividend": "dividend",
    "bonus": "bonus",
}
# Case-insensitive exchange lookup that preserves canonical casing ("MFCentral").
_EXCHANGE_BY_LOWER: dict[str, str] = {e.lower(): e for e in get_args(ExchangeStr)}
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%Y/%m/%d",
    "%d-%b-%Y",
    "%d-%b-%y",
    "%d-%B-%Y",
)

_ZIP_MAGIC = b"PK\x03\x04"
_OLE2_XLS_MAGIC = b"\xd0\xcf\x11\xe0"
_SUMMARY_SYMBOLS = frozenset({"TOTAL", "GRAND TOTAL", "SUBTOTAL", "SUMMARY"})
MAX_IMPORT_ROWS = 25_000


@dataclass(frozen=True, slots=True)
class ParsedInvestmentRow:
    """One canonical CSV/Excel row → one ``investment_transaction`` (unsigned magnitudes).

    ``line_no`` is the 1-based source row (header or physical line) — carried so the import
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
    """File-level CSV/Excel failure: bad encoding, missing/duplicate required column,
    or zero importable rows. Maps to HTTP 422 at the route."""


class UnsupportedFileFormatError(CSVParseError):
    """File format is recognized but unsupported (e.g. legacy binary .xls)."""


def _normalize_header(header: str) -> str:
    """Normalize header text: strip, lowercase, replace spaces/dots/hyphens with underscores."""
    s = header.strip().lower()
    return re.sub(r"[\s\.\-]+", "_", s)


def _detect_format(file_bytes: bytes) -> str:
    """Detect whether file is OpenXML Excel, legacy .xls, or text CSV via magic bytes."""
    if file_bytes.startswith(_ZIP_MAGIC):
        return "xlsx"
    if file_bytes.startswith(_OLE2_XLS_MAGIC):
        return "xls"
    return "csv"


def _to_paise(amount: Decimal) -> int:
    return int((amount * 100).to_integral_value(rounding=ROUND_HALF_EVEN))


def _q8(value: Decimal) -> Decimal:
    """Quantize to 8 dp — the scaled-int storage precision — at the boundary."""
    return value.quantize(Decimal("1e-8"), rounding=ROUND_HALF_EVEN)


def _to_decimal(val: object) -> Decimal:
    """Parse a numeric cell (commas/spaces stripped). Raises ``InvalidOperation``."""
    if isinstance(val, (int, Decimal)):
        return Decimal(val)
    if isinstance(val, float):
        return Decimal(str(val))
    text = str(val).replace(",", "").replace(" ", "").replace("₹", "").replace("$", "")
    return Decimal(text)


def _try_date(val: object) -> date_t | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date_t):
        return val
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        # Excel serial date: 20000 (~1954) to 60000 (~2064)
        if 20000 <= val <= 60000:
            try:
                converted = openpyxl.utils.datetime.from_excel(val)
                if isinstance(converted, (datetime, date_t)):
                    return converted.date() if isinstance(converted, datetime) else converted
            except Exception:
                pass
        return None
    text = str(val).strip()
    if not text:
        return None
    # Check ISO format e.g. "2024-03-15T09:30:00" or "2024-03-15 09:30:00"
    if "T" in text or " " in text:
        iso_candidate = text.replace("T", " ").split()[0]
        try:
            return datetime.strptime(iso_candidate, "%Y-%m-%d").date()
        except ValueError:
            pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _is_summary_or_blank(raw_symbol: str | None, values: dict[str, object]) -> bool:
    """Identify trailing blank lines or summary/total rows to avoid poisoning batch status."""
    if not any(v is not None and str(v).strip() for v in values.values()):
        return True
    raw_name = str(values.get("name") or "").strip()
    is_summary_text = bool(
        (raw_symbol and raw_symbol.upper() in _SUMMARY_SYMBOLS)
        or (raw_name and raw_name.upper() in _SUMMARY_SYMBOLS)
    )
    return bool(is_summary_text and (values.get("date") is None or values.get("type") is None))


def parse_investment_csv(
    file_bytes: bytes, *, default_asset_class: AssetClassStr
) -> tuple[list[ParsedInvestmentRow], list[str]]:
    """Parse a canonical investment CSV or Excel file into rows + per-row skip warnings.

    ``default_asset_class`` is applied to rows without an ``asset_class`` column/value
    (e.g. a single-asset Zerodha tradebook, where the user picks the class once in the
    upload form). Per-type magnitude validation (buy needs units+price, etc.) is the
    import service's boundary; this parser does structural + vocabulary + currency checks.

    Raises:
        UnsupportedFileFormatError: legacy binary .xls file detected.
        CSVParseError: bad encoding, missing/duplicate required column, or zero
            importable rows.
    """
    fmt = _detect_format(file_bytes)
    if fmt == "xls":
        raise UnsupportedFileFormatError(
            "legacy .xls format is not supported; please save or export as .xlsx or .csv"
        )
    if fmt == "xlsx":
        return _parse_excel(file_bytes, default_asset_class=default_asset_class)
    return _parse_csv(file_bytes, default_asset_class=default_asset_class)


# Alias for caller clarity
parse_investment_file = parse_investment_csv


def _parse_csv(
    file_bytes: bytes, *, default_asset_class: AssetClassStr
) -> tuple[list[ParsedInvestmentRow], list[str]]:
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise CSVParseError("file is not valid UTF-8 text") from e

    # Tolerate leading blank lines, then scan for header row (up to 50 rows)
    raw_lines = text.splitlines()
    header_idx = None
    col_map: dict[str, str] = {}

    for idx, line in enumerate(raw_lines[:50]):
        if not line.strip():
            continue
        row = list(csv.reader([line]))[0]
        candidate_col_map: dict[str, str] = {}
        dup_found = False
        for cell in row:
            if not cell.strip():
                continue
            norm_header = _normalize_header(cell)
            field = _ALIAS_TO_FIELD.get(norm_header)
            if field is None:
                continue
            if field in candidate_col_map:
                dup_found = True
                break
            candidate_col_map[field] = cell
        if dup_found:
            raise CSVParseError(f"duplicate column for {field!r}")
        if candidate_col_map.keys() >= _REQUIRED_FIELDS:
            header_idx = idx
            col_map = candidate_col_map
            break

    if header_idx is None:
        missing = _REQUIRED_FIELDS - col_map.keys()
        raise CSVParseError(f"missing required column(s): {', '.join(sorted(missing))}")

    # Re-parse data lines starting after header_idx
    reader = csv.DictReader(io.StringIO("\n".join(raw_lines[header_idx:])))
    rows: list[ParsedInvestmentRow] = []
    warnings: list[str] = []

    for line_no, raw in enumerate(reader, start=header_idx + 2):  # 1-based physical line number
        if line_no > MAX_IMPORT_ROWS:
            raise CSVParseError("file exceeds maximum allowed rows (25,000)")
        values = {
            field: ((raw.get(header) or "").strip() or None) for field, header in col_map.items()
        }
        raw_sym = values.get("symbol")
        if _is_summary_or_blank(raw_sym, cast("dict[str, object]", values)):
            continue

        result = _parse_row(values, line_no=line_no, default_asset_class=default_asset_class)
        if isinstance(result, str):
            warnings.append(result)
        else:
            rows.append(result)

    if not rows:
        raise CSVParseError("no valid investment transactions found")
    return rows, warnings


def _parse_excel(
    file_bytes: bytes, *, default_asset_class: AssetClassStr
) -> tuple[list[ParsedInvestmentRow], list[str]]:
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    except Exception as e:
        raise CSVParseError("could not parse Excel file") from e

    try:
        # Multi-sheet discovery: check wb.active first, then other sheets
        target_sheet = None
        header_row_idx = None
        col_map: dict[str, int] = {}

        candidate_sheets = []
        if wb.active is not None:
            candidate_sheets.append(wb.active)
        for name in wb.sheetnames:
            sheet = wb[name]
            if sheet not in candidate_sheets:
                candidate_sheets.append(sheet)

        for sheet in candidate_sheets:
            for r_idx, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                if r_idx > 50:
                    break
                if not row or not any(row):
                    continue
                candidate_col_map: dict[str, int] = {}
                dup_found = False
                for c_idx, cell in enumerate(row):
                    if cell is None:
                        continue
                    norm_header = _normalize_header(str(cell))
                    field = _ALIAS_TO_FIELD.get(norm_header)
                    if field is None:
                        continue
                    if field in candidate_col_map:
                        dup_found = True
                        break
                    candidate_col_map[field] = c_idx
                if dup_found:
                    raise CSVParseError(f"duplicate column for {field!r}")
                if candidate_col_map.keys() >= _REQUIRED_FIELDS:
                    target_sheet = sheet
                    header_row_idx = r_idx
                    col_map = candidate_col_map
                    break
            if target_sheet is not None:
                break

        if target_sheet is None or header_row_idx is None:
            missing = _REQUIRED_FIELDS - col_map.keys()
            raise CSVParseError(f"missing required column(s): {', '.join(sorted(missing))}")

        rows: list[ParsedInvestmentRow] = []
        warnings: list[str] = []

        for line_no, row in enumerate(target_sheet.iter_rows(values_only=True), start=1):
            if line_no <= header_row_idx:
                continue
            if line_no > MAX_IMPORT_ROWS:
                raise CSVParseError("file exceeds maximum allowed rows (25,000)")
            if not row or not any(row):
                continue

            values: dict[str, object] = {
                field: row[c_idx] if c_idx < len(row) else None for field, c_idx in col_map.items()
            }
            raw_sym = str(values.get("symbol") or "").strip() or None
            if _is_summary_or_blank(raw_sym, values):
                continue

            result = _parse_row(values, line_no=line_no, default_asset_class=default_asset_class)
            if isinstance(result, str):
                warnings.append(result)
            else:
                rows.append(result)

        if not rows:
            raise CSVParseError("no valid investment transactions found")
        return rows, warnings
    finally:
        wb.close()


def _parse_row(
    values: dict[str, object], *, line_no: int, default_asset_class: AssetClassStr
) -> ParsedInvestmentRow | str:
    """Build one row, or return a PII-safe skip reason (line number + cause only).

    Currency resolves to INR (the default when the ``currency`` column is absent) or USD;
    any other currency is a per-row skip. A US-class row (``us_equity``/``us_etf``) resolving
    to INR is also skipped — Yahoo prices it in USD, so an INR stamp would value it 1:1 — and
    so is an INR-only row (``indian_equity``/``indian_mf``/``nps``) resolving to USD.
    """
    raw_symbol_val = values.get("symbol")
    raw_symbol = str(raw_symbol_val).strip() if raw_symbol_val is not None else ""
    if not raw_symbol:
        return f"row {line_no}: missing symbol"
    symbol = raw_symbol.upper()

    isin_val = values.get("isin")
    isin = str(isin_val).strip().upper() if isin_val is not None and str(isin_val).strip() else None

    curr_val = values.get("currency")
    curr_str = str(curr_val).strip() if curr_val is not None and str(curr_val).strip() else "INR"
    currency = curr_str.upper()
    if currency not in ("INR", "USD"):
        return f"row {line_no}: unsupported currency {currency} (INR/USD only)"

    type_val = values.get("type")
    type_raw = str(type_val).strip().lower() if type_val is not None else ""
    if not type_raw:
        return f"row {line_no}: missing transaction type"
    if type_raw in _CSV_DISALLOWED_TYPES:
        return f"row {line_no}: unsupported transaction type"
    txn_type = _TXN_TYPE_SYNONYMS.get(type_raw)
    if txn_type is None:
        return f"row {line_no}: unsupported transaction type"

    txn_date = _try_date(values.get("date"))
    if txn_date is None:
        return f"row {line_no}: invalid or missing date"

    units_raw = values.get("units")
    price_raw = values.get("price")
    amount_raw = values.get("amount")
    fees_raw = values.get("fees")
    try:
        units = (
            _q8(_to_decimal(units_raw))
            if units_raw is not None and str(units_raw).strip() != ""
            else Decimal("0")
        )
        price = (
            _q8(_to_decimal(price_raw))
            if price_raw is not None and str(price_raw).strip() != ""
            else None
        )
        fees_paise = (
            _to_paise(_to_decimal(fees_raw))
            if fees_raw is not None and str(fees_raw).strip() != ""
            else 0
        )
        if amount_raw is not None and str(amount_raw).strip() != "":
            amount_paise = _to_paise(_to_decimal(amount_raw))
        elif price is not None and units > 0:
            amount_paise = _to_paise(units * price)
        else:
            amount_paise = 0
    except InvalidOperation:
        return f"row {line_no}: invalid number"

    ex_val = values.get("exchange")
    ex_str = str(ex_val).strip() if ex_val is not None and str(ex_val).strip() else "OTHER"
    exchange = _EXCHANGE_BY_LOWER.get(ex_str.lower())
    if exchange is None:
        return f"row {line_no}: unknown exchange"

    ac_val = values.get("asset_class")
    ac_str = (
        str(ac_val).strip().lower()
        if ac_val is not None and str(ac_val).strip()
        else default_asset_class.lower()
    )
    if ac_str not in _ASSET_CLASSES:
        return f"row {line_no}: unknown asset_class"
    allowed = class_currencies(cast("AssetClassStr", ac_str))
    if currency not in allowed:
        return f"row {line_no}: {ac_str} requires currency={allowed[0]}"

    name_val = values.get("name")
    name_str = str(name_val).strip() if name_val is not None and str(name_val).strip() else symbol

    return ParsedInvestmentRow(
        line_no=line_no,
        symbol=symbol,
        isin=isin,
        name=name_str,
        asset_class=cast("AssetClassStr", ac_str),
        exchange=cast("ExchangeStr", exchange),
        currency=currency,
        date=txn_date,
        txn_type=txn_type,
        units=units,
        price=price,
        amount_native_paise=amount_paise,
        fees_native_paise=fees_paise,
    )
