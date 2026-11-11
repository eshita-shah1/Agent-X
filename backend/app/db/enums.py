import enum


class InstrumentType(str, enum.Enum):
    EQUITY = "EQUITY"
    # Futures are disabled until genuine contract-specific data exists. The value exists so
    # the instrument model does not need to change later; no futures table exists yet.
    FUTURE = "FUTURE"


class Timeframe(str, enum.Enum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M60 = "60m"
    D1 = "1d"


class PriceBasis(str, enum.Enum):
    """What a stored candle's prices are known to be.

    Data is never labelled as-traded until a documented verification has justified it.
    """

    UNVERIFIED_PROVIDER_NATIVE = "UNVERIFIED_PROVIDER_NATIVE"
    SPLIT_ADJUSTED_VERIFIED = "SPLIT_ADJUSTED_VERIFIED"
    AS_TRADED_VERIFIED = "AS_TRADED_VERIFIED"


class AdjustmentMethod(str, enum.Enum):
    SPLIT = "SPLIT"
    SPLIT_AND_DIVIDEND = "SPLIT_AND_DIVIDEND"
    PROVIDER_ADJUSTED = "PROVIDER_ADJUSTED"


class CorporateActionType(str, enum.Enum):
    SPLIT = "SPLIT"
    BONUS = "BONUS"
    DIVIDEND = "DIVIDEND"
    OTHER = "OTHER"


class TradingStatus(str, enum.Enum):
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"


class VerificationStatus(str, enum.Enum):
    UNVERIFIED = "UNVERIFIED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"


class JobType(str, enum.Enum):
    BACKFILL = "BACKFILL"
    INCREMENTAL = "INCREMENTAL"


class JobStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"  # some instruments succeeded, some failed
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"  # the worker died or lost its heartbeat


class BatchStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    NO_DATA = "NO_DATA"
    FAILED = "FAILED"


class CalendarDayKind(str, enum.Enum):
    HOLIDAY = "HOLIDAY"
    SPECIAL_SESSION = "SPECIAL_SESSION"

# ---- Phase 4: risk engine and Mock Exchange -------------------------------------------------


class OrderSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, enum.Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


OPEN_ORDER_STATUSES = (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED)
TERMINAL_ORDER_STATUSES = (OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED)


class OrderSource(str, enum.Enum):
    MANUAL = "MANUAL"  # submitted by the user through the API
    AGENT = "AGENT"  # submitted by the autonomous agent (Phase 5)
    RISK_EXIT = "RISK_EXIT"  # protective stop-loss / take-profit exit


class RiskOutcome(str, enum.Enum):
    APPROVED = "APPROVED"
    RESIZED = "RESIZED"
    REJECTED = "REJECTED"


class LedgerEntryType(str, enum.Enum):
    BUY_SETTLEMENT = "BUY_SETTLEMENT"
    SELL_PROCEEDS = "SELL_PROCEEDS"
    FEE = "FEE"
