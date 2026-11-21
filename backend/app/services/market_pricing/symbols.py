"""Turn a fin-tracker ``price_key`` into provider routing.

Supported keys
--------------
``INF879O01027``      Indian mutual fund ISIN  -> mfapi (AMFI code looked up from NAVAll)
``AMFI:122639``       Mutual fund by AMFI scheme code -> mfapi
``NSE:RELIANCE``      -> Yahoo ``RELIANCE.NS``
``BSE:500325``        -> Yahoo ``500325.BO``
``NASDAQ:NVDA``       -> Yahoo ``NVDA``   (also NYSE, NYSEARCA, AMEX, BATS)
``FX:USDINR``         -> Yahoo ``USDINR=X`` (price is INR per 1 USD)
``INDEX:NSEI``        -> Yahoo ``^NSEI``
``YAHOO:<ticker>``    escape hatch, ticker used verbatim
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_US_EXCHANGES = {"NASDAQ", "NYSE", "NYSEARCA", "AMEX", "BATS", "ARCA"}


class UnsupportedPriceKey(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedKey:
    price_key: str
    asset_class: str  # mf | equity | fx | index
    source: str  # provider name, also the rate-limit host key
    provider_symbol: str | None  # None = resolve later (MF ISIN -> AMFI code)
    currency: str


def is_isin(value: str) -> bool:
    return bool(_ISIN.match(value))


def resolve_price_key(price_key: str) -> ResolvedKey:
    key = price_key.strip()
    if not key or len(key) > 64:
        raise UnsupportedPriceKey(f"invalid price_key: {price_key!r}")

    if ":" not in key:
        upper = key.upper()
        if is_isin(upper) and upper.startswith("INF"):
            return ResolvedKey(upper, "mf", "mfapi", None, "INR")
        raise UnsupportedPriceKey(
            f"{price_key!r}: bare keys must be Indian mutual fund ISINs (INF...); "
            "use EXCHANGE:SYMBOL for everything else"
        )

    prefix, _, symbol = key.partition(":")
    prefix = prefix.upper()
    symbol = symbol.strip()
    if not symbol:
        raise UnsupportedPriceKey(f"{price_key!r}: empty symbol")

    if prefix == "YAHOO":  # verbatim, case preserved (e.g. 'YAHOO:BRK-B')
        return ResolvedKey(f"YAHOO:{symbol}", "equity", "yahoo", symbol, "USD")

    symbol = symbol.upper()
    canonical = f"{prefix}:{symbol}"
    if prefix == "AMFI":
        if not symbol.isdigit():
            raise UnsupportedPriceKey(f"{price_key!r}: AMFI scheme codes are numeric")
        return ResolvedKey(canonical, "mf", "mfapi", symbol, "INR")
    if prefix == "NSE":
        return ResolvedKey(canonical, "equity", "yahoo", f"{symbol}.NS", "INR")
    if prefix == "BSE":
        return ResolvedKey(canonical, "equity", "yahoo", f"{symbol}.BO", "INR")
    if prefix in _US_EXCHANGES:
        # Yahoo writes share classes with a dash: BRK.B -> BRK-B
        return ResolvedKey(canonical, "equity", "yahoo", symbol.replace(".", "-"), "USD")
    if prefix == "FX":
        if len(symbol) != 6 or not symbol.isalpha():
            raise UnsupportedPriceKey(f"{price_key!r}: FX keys look like FX:USDINR")
        return ResolvedKey(canonical, "fx", "yahoo", f"{symbol}=X", symbol[3:])
    if prefix == "INDEX":
        ticker = symbol if symbol.startswith("^") else f"^{symbol}"
        currency = "INR" if ticker in {"^NSEI", "^BSESN", "^NSEBANK", "^CRSLDX"} else "USD"
        return ResolvedKey(f"INDEX:{ticker[1:]}", "index", "yahoo", ticker, currency)
    raise UnsupportedPriceKey(f"{price_key!r}: unknown exchange prefix {prefix!r}")
