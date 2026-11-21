"""SQLAlchemy ORM models. One file per logical table (per PRD §Data model)."""

from app.models.account import Account, AccountTypeStr, CurrencyStr
from app.models.base import Base
from app.models.benchmark import Benchmark, BenchmarkKindStr, BenchmarkNav
from app.models.category import Category, CategoryKindStr
from app.models.fx_rate_quote import FxRateQuote
from app.models.import_batch import ImportBatch, ImportStatusStr
from app.models.instrument import AssetClassStr, ExchangeStr, Instrument
from app.models.investment_account import InvestmentAccount, InvestmentAccountTypeStr
from app.models.investment_transaction import InvestmentTransaction, InvestmentTxnTypeStr
from app.models.label import Label
from app.models.market_pricing import (
    MarketCorporateAction,
    MarketHostState,
    MarketInstrument,
    MarketInstrumentPin,
    MarketPriceCoverage,
    MarketPriceHistory,
    MarketPriceJob,
    MarketTaskRun,
)
from app.models.merchant_alias import MerchantAlias
from app.models.merchant_label_map import MerchantLabelMap
from app.models.merchant_tag_map import MerchantTagMap
from app.models.peer_contact import PeerContact
from app.models.peer_entry import PeerDirectionStr, PeerEntry
from app.models.portfolio_daily_snapshot import PortfolioDailySnapshot
from app.models.realised_lot import RealisedLot
from app.models.session import RefreshSession
from app.models.transaction import Transaction, TransactionSourceStr, TransactionTypeStr
from app.models.transaction_label import TransactionLabel
from app.models.user import User

__all__ = [
    "Account",
    "AccountTypeStr",
    "AssetClassStr",
    "Base",
    "Benchmark",
    "BenchmarkKindStr",
    "BenchmarkNav",
    "Category",
    "CategoryKindStr",
    "CurrencyStr",
    "ExchangeStr",
    "FxRateQuote",
    "ImportBatch",
    "ImportStatusStr",
    "Instrument",
    "InvestmentAccount",
    "InvestmentAccountTypeStr",
    "InvestmentTransaction",
    "InvestmentTxnTypeStr",
    "Label",
    "MarketCorporateAction",
    "MarketHostState",
    "MarketInstrument",
    "MarketInstrumentPin",
    "MarketPriceCoverage",
    "MarketPriceHistory",
    "MarketPriceJob",
    "MarketTaskRun",
    "MerchantAlias",
    "MerchantLabelMap",
    "MerchantTagMap",
    "PeerContact",
    "PeerDirectionStr",
    "PeerEntry",
    "PortfolioDailySnapshot",
    "RealisedLot",
    "RefreshSession",
    "Transaction",
    "TransactionLabel",
    "TransactionSourceStr",
    "TransactionTypeStr",
    "User",
]
