"""Provider-independent data types shared by adapters, the MarketDataService and the API.

Adapters return these. Nothing outside `providers/` ever sees a provider-specific response format.
"""

import enum
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


class Capability(str, enum.Enum):
    HISTORICAL_DAILY_CANDLES = "HISTORICAL_DAILY_CANDLES"
    HISTORICAL_INTRADAY_CANDLES = "HISTORICAL_INTRADAY_CANDLES"
    FIVE_MINUTE_INTERVAL = "FIVE_MINUTE_INTERVAL"
    LATEST_CANDLE = "LATEST_CANDLE"
    CORPORATE_ACTIONS = "CORPORATE_ACTIONS"
    QUOTE_RETRIEVAL = "QUOTE_RETRIEVAL"
    INSTRUMENT_METADATA = "INSTRUMENT_METADATA"
    ADJUSTED_INTRADAY_PRICES = "ADJUSTED_INTRADAY_PRICES"
    FUTURES_CONTRACT_DATA = "FUTURES_CONTRACT_DATA"


class CapabilityStatus(str, enum.Enum):
    """How much is known about a capability. A provider must never claim more than this."""

    DOCUMENTED = "DOCUMENTED"  # stated in the library/provider documentation, not tested here
    VERIFIED = "VERIFIED"  # demonstrated by an actual probe run (evidence stored)
    UNSUPPORTED = "UNSUPPORTED"  # a probe showed it does not work / is not offered
    UNKNOWN = "UNKNOWN"  # nothing established


class DataStatus(str, enum.Enum):
    UNKNOWN = "UNKNOWN"  # nothing has been observed yet; never reported as healthy
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    INCOMPLETE = "INCOMPLETE"


class IssueType(str, enum.Enum):
    NAIVE_TIMESTAMP = "NAIVE_TIMESTAMP"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    MISALIGNED_TIMESTAMP = "MISALIGNED_TIMESTAMP"
    OUT_OF_SESSION = "OUT_OF_SESSION"
    NON_NUMERIC_VALUE = "NON_NUMERIC_VALUE"
    NON_FINITE_VALUE = "NON_FINITE_VALUE"
    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    INVALID_OHLC = "INVALID_OHLC"
    INVALID_VOLUME = "INVALID_VOLUME"
    DUPLICATE_IDENTICAL = "DUPLICATE_IDENTICAL"
    DUPLICATE_CONFLICTING = "DUPLICATE_CONFLICTING"
    INCOMPLETE_CANDLE = "INCOMPLETE_CANDLE"
    UNEXPECTED_INTERVAL = "UNEXPECTED_INTERVAL"
    MISSING_INTERVALS = "MISSING_INTERVALS"
    RESTATEMENT = "RESTATEMENT"
    CORPORATE_ACTIONS_UNAVAILABLE = "CORPORATE_ACTIONS_UNAVAILABLE"
    UNRECOVERABLE_GAP = "UNRECOVERABLE_GAP"
    WINDOW_EDGE_TRUNCATED = "WINDOW_EDGE_TRUNCATED"


class Severity(str, enum.Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class RawBar:
    """One provider row, as delivered (floats, possibly NaN; timestamp possibly naive)."""

    start_ts: datetime | None
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    extras: dict = field(default_factory=dict)  # provider-specific passthrough: adj_close, dividends, splits...


@dataclass(frozen=True)
class RawCorporateAction:
    ex_date: date
    kind: str  # "DIVIDEND" | "SPLIT"  (SPLIT value is the provider's new:old ratio, e.g. 2.0 for 2-for-1)
    value: float


@dataclass
class ProviderBarsResult:
    provider_code: str
    provider_symbol: str
    interval: str
    request_start: datetime
    request_end: datetime
    bars: list[RawBar]
    observed_at: datetime  # when Agent-X received the response (tz-aware UTC)
    reported_timezone: str | None = None  # exchange timezone as reported by the provider, if any
    meta: dict = field(default_factory=dict)  # request parameters, library version, column list...


@dataclass
class ProviderQuote:
    provider_symbol: str
    last_price: Decimal | None
    currency: str | None
    observed_at: datetime
    market_ts: datetime | None  # None = the provider did not say when this price occurred
    meta: dict = field(default_factory=dict)


@dataclass
class ProviderInstrumentMetadata:
    provider_symbol: str
    exchange: str | None
    currency: str | None
    timezone: str | None
    name: str | None
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedCandle:
    """A validated, normalised, finalised candle ready for archiving (UTC instants, Decimal prices)."""

    instrument_id: int
    provider_code: str
    provider_symbol: str
    timeframe: str
    bar_start_ts: datetime  # tz-aware UTC
    bar_end_ts: datetime
    session_date: date  # exchange-local trading date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    observed_at: datetime
    source_metadata: dict = field(default_factory=dict)


@dataclass
class QualityIssue:
    issue_type: IssueType
    severity: Severity
    bar_start_ts: datetime | None
    message: str
    details: dict = field(default_factory=dict)
