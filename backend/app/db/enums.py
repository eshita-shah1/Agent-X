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


class BacktestRunStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"  # refused at run time (e.g. the data changed) or crashed; nothing was stored
    INTERRUPTED = "INTERRUPTED"  # the worker died or lost its heartbeat; nothing was stored


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


class ExecutionPolicy(str, enum.Enum):
    """Which execution-price policy produced an order's fills. Recorded on orders and fills, never inferred."""

    # Bars whose basis was verified with evidence and approved (decision D4).
    VERIFIED_NEXT_BAR_OPEN = "VERIFIED_NEXT_BAR_OPEN"
    # Retrospective simulation on UNVERIFIED_PROVIDER_NATIVE bars (e.g. Yahoo Finance). Not verified execution.
    SIMULATION_PROVIDER_NATIVE = "SIMULATION_PROVIDER_NATIVE"


EXECUTION_MODE_POLICY = {
    "verified_next_bar_open": ExecutionPolicy.VERIFIED_NEXT_BAR_OPEN,
    "simulation_provider_native": ExecutionPolicy.SIMULATION_PROVIDER_NATIVE,
}


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
    CASH_IN_LIEU = "CASH_IN_LIEU"  # fractional entitlement of a corporate action paid in cash (migration 0007)
    DIVIDEND = "DIVIDEND"  # operator-confirmed gross cash dividend (migration 0008)


class DividendCreditStatus(str, enum.Enum):
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"


class Product(str, enum.Enum):
    """NSE cash-equity product. Costs are selected per product; only DELIVERY orders are executed today
    (intraday square-off and margin are not modelled)."""

    DELIVERY = "DELIVERY"
    INTRADAY = "INTRADAY"


class AdjustmentStatus(str, enum.Enum):
    APPLIED = "APPLIED"  # the position was adjusted; valuation and exits resume
    REJECTED = "REJECTED"  # validation failed; the position stays frozen
    BLOCKED_FRACTIONAL = "BLOCKED_FRACTIONAL"  # a fractional entitlement needs a cash-in-lieu price; stays frozen


# ---- Phase 5: autonomous agent orchestration (migration 0012) -------------------------------------------------------


class AgentStatus(str, enum.Enum):
    """Persisted lifecycle of an account's agent. Entry blocking by the kill switch is NOT a status: the Phase 4
    `risk_state.kill_switch_engaged` flag stays the only switch (approved decision D11)."""

    STOPPED = "STOPPED"  # no strategy evaluation, no agent orders; Phase 4 protective exits are still run (D10)
    RUNNING = "RUNNING"  # evaluation, entries and exits
    PAUSED = "PAUSED"  # monitor mode: evaluation and exits continue, entries are suppressed
    ERROR = "ERROR"  # reconciliation or repeated cycle failure; no agent orders until resumed or stopped


class AgentMode(str, enum.Enum):
    EXECUTION = "EXECUTION"  # orders go through OrderService (execution was available when the agent started)
    SIGNALS_ONLY = "SIGNALS_ONLY"  # execution was disabled at start: signals are recorded, no order is ever submitted


class AgentCycleStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"  # the worker died during the cycle (found by the next cycle)


class AgentInstrumentStatus(str, enum.Enum):
    OK = "OK"
    MANUAL_INTERVENTION = "MANUAL_INTERVENTION"  # a manual order/fill or an adjustment touched the instrument (D7)
    ORPHANED_OVERNIGHT = "ORPHANED_OVERNIGHT"  # an agent position survived its session (D2): never carried over
    DATA_ERROR = "DATA_ERROR"  # the archive could not be replayed for this instrument


class AgentDecisionKind(str, enum.Enum):
    SELECTION = "SELECTION"  # the locked session selection for one instrument
    EVALUATION = "EVALUATION"  # one finalized bar evaluated by the acting configuration
    OWED_EXIT = "OWED_EXIT"  # a submission of an exit still owed from an earlier signal (D6)


class AgentDecisionOutcome(str, enum.Enum):
    SELECTED = "SELECTED"
    NO_SELECTION = "NO_SELECTION"
    NO_SIGNAL = "NO_SIGNAL"
    ENTRY_SUBMITTED = "ENTRY_SUBMITTED"  # the order's own status says whether risk/exchange accepted it
    EXIT_SUBMITTED = "EXIT_SUBMITTED"
    EXIT_OWED = "EXIT_OWED"  # exit signal recorded; submitted when an eligible price exists
    EXIT_ALREADY_OWED = "EXIT_ALREADY_OWED"
    SIGNALS_ONLY_NOT_SUBMITTED = "SIGNALS_ONLY_NOT_SUBMITTED"
    SUPPRESSED_PAUSED = "SUPPRESSED_PAUSED"
    SUPPRESSED_KILL_SWITCH = "SUPPRESSED_KILL_SWITCH"
    SUPPRESSED_INSTRUMENT_BLOCKED = "SUPPRESSED_INSTRUMENT_BLOCKED"
    SUPPRESSED_POSITION_OR_ORDER = "SUPPRESSED_POSITION_OR_ORDER"
    SUPPRESSED_LATE_SIGNAL = "SUPPRESSED_LATE_SIGNAL"  # not the latest eligible bar (D5)
    SUPPRESSED_PRICE_NOT_ELIGIBLE = "SUPPRESSED_PRICE_NOT_ELIGIBLE"
    SUPPRESSED_INVALID_LEVELS = "SUPPRESSED_INVALID_LEVELS"
    SUPPRESSED_NO_QUANTITY = "SUPPRESSED_NO_QUANTITY"
    SUPPRESSED_NOT_HELD = "SUPPRESSED_NOT_HELD"  # an exit signal with no agent position to exit
