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
