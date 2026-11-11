"""Backtest inputs, result records and labels (Phase 3, Step 4A).

Pure data: no database, API, account or execution code. Prices and money are Decimal; indicator inputs are derived
from them as float64 inside the engine. Every record is frozen.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from app.backtest.costs import CostBasis, CostModel, to_decimal
from app.marketdata.calendar import EXCHANGE_TZ
from app.strategies.selection import Candidate, PinnedPolicy, Policy, RegimePriorityPolicy, configuration_problem

ENGINE_VERSION = "1"
FILL_MODEL = "NEXT_BAR_OPEN_V1"
SIZING_MODEL = "WHOLE_SHARES_FULL_ALLOCATION_V1"
PRICE_BASIS = "UNVERIFIED_PROVIDER_NATIVE"
TIMEFRAME = "5m"
DEFAULT_WARMUP_SESSIONS = 2
MAX_WARMUP_SESSIONS = 20
REFUSED_ACTION_TYPES = frozenset({"SPLIT", "BONUS"})  # decision D11
KNOWN_ACTION_TYPES = frozenset({"SPLIT", "BONUS", "DIVIDEND", "OTHER"})


class RefusalCode(str, enum.Enum):
    INVALID_CONFIGURATION = "INVALID_CONFIGURATION"
    INVALID_PERIOD = "INVALID_PERIOD"
    INVALID_DATA = "INVALID_DATA"
    NO_DATA = "NO_DATA"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    CORPORATE_ACTION_IN_PERIOD = "CORPORATE_ACTION_IN_PERIOD"


class BacktestRefused(ValueError):
    """The backtest cannot be run as requested. Raised before any simulation starts."""

    def __init__(self, code: RefusalCode, message: str, instrument_id: Any = None) -> None:
        super().__init__(f"{code.value}: {message}" + ("" if instrument_id is None else f" (instrument {instrument_id})"))
        self.code = code
        self.message = message
        self.instrument_id = instrument_id


class CapitalSource(str, enum.Enum):
    ACCOUNT_INITIAL_CAPITAL = "ACCOUNT_INITIAL_CAPITAL"
    REQUEST_OVERRIDE = "REQUEST_OVERRIDE"


class ExitReason(str, enum.Enum):
    STOP = "STOP"  # touched intrabar: at the stop level
    STOP_GAP = "STOP_GAP"  # bar opened at or through the stop: at the open
    TARGET = "TARGET"  # touched intrabar, or opened above it: at the target level (conservative, decision 1)
    STRATEGY_EXIT = "STRATEGY_EXIT"  # the strategy's own exit signal: at the next available bar's open
    SESSION_SQUARE_OFF = "SESSION_SQUARE_OFF"  # square-off signal (bar ending 15:15): at the next bar's open
    SQUARE_OFF_FALLBACK_LAST_BAR = "SQUARE_OFF_FALLBACK_LAST_BAR"  # D5 fallback: at the last available bar's close


class ExitTiming(str, enum.Enum):
    AT_OPEN = "AT_OPEN"
    INTRABAR = "INTRABAR"
    AT_CLOSE = "AT_CLOSE"


class EventType(str, enum.Enum):
    ENTRY_SIGNAL = "ENTRY_SIGNAL"
    EXIT_SIGNAL = "EXIT_SIGNAL"
    ENTRY_EXPIRED_MISSING_BAR = "ENTRY_EXPIRED_MISSING_BAR"
    ENTRY_EXPIRED_SESSION_END = "ENTRY_EXPIRED_SESSION_END"
    ENTRY_SKIPPED_INSUFFICIENT_CAPITAL = "ENTRY_SKIPPED_INSUFFICIENT_CAPITAL"
    EXIT_DEFERRED_MISSING_BAR = "EXIT_DEFERRED_MISSING_BAR"
    MISSING_BARS = "MISSING_BARS"
    NO_SELECTION_DECISION = "NO_SELECTION_DECISION"


class BenchmarkStatus(str, enum.Enum):
    VALID = "VALID"
    LIMITED = "LIMITED"


class WarmupSource(str, enum.Enum):
    NONE = "NONE"
    PRIOR_SESSIONS = "PRIOR_SESSIONS"
    SHIFTED_INTO_PERIOD = "SHIFTED_INTO_PERIOD"
    PRIOR_AND_SHIFTED = "PRIOR_AND_SHIFTED"


# ---- inputs -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BacktestBar:
    """One finalized archived candle. `source_ref` optionally identifies the stored row (e.g. candle id and
    observation version) so the data fingerprint pins the exact observation used."""

    start_ts: datetime
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    source_ref: tuple | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.start_ts, datetime) or self.start_ts.tzinfo is None or self.start_ts.utcoffset() is None:
            raise ValueError(f"start_ts must be a timezone-aware datetime, got {self.start_ts!r}")
        # One representation per instant: equal instants given in UTC, IST or any other zone become identical bars,
        # so fingerprints, explanations and outcomes never depend on the input's time zone. Session dates and times
        # are still derived in exchange time (IST) wherever they are needed.
        object.__setattr__(self, "start_ts", self.start_ts.astimezone(UTC))
        if self.start_ts.astimezone(EXCHANGE_TZ).date() != self.session_date:
            raise ValueError(f"session_date {self.session_date} does not match start_ts {self.start_ts.isoformat()}")
        for name in ("open", "high", "low", "close"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise ValueError(f"{name} must be a Decimal, got {type(value).__name__}")
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a finite price > 0, got {value}")
        if isinstance(self.volume, bool) or not isinstance(self.volume, int) or self.volume < 0:
            raise ValueError(f"volume must be an integer >= 0, got {self.volume!r}")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError(f"inconsistent OHLC: {self.open}/{self.high}/{self.low}/{self.close}")
        if self.source_ref is not None and not isinstance(self.source_ref, tuple):
            raise ValueError("source_ref must be a tuple or None")


@dataclass(frozen=True)
class CorporateActionRecord:
    ex_date: date
    action_type: str  # SPLIT | BONUS | DIVIDEND | OTHER
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.ex_date, date) or isinstance(self.ex_date, datetime):
            raise ValueError("ex_date must be a date")
        if self.action_type not in KNOWN_ACTION_TYPES:
            raise ValueError(f"action_type must be one of {sorted(KNOWN_ACTION_TYPES)}, got {self.action_type!r}")


@dataclass(frozen=True)
class InstrumentData:
    instrument_id: Any
    bars: tuple[BacktestBar, ...]
    corporate_actions: tuple[CorporateActionRecord, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.bars, tuple) or not isinstance(self.corporate_actions, tuple):
            raise ValueError("bars and corporate_actions must be tuples")


def policy_candidates(policy: Policy) -> tuple[Candidate, ...]:
    return policy.ordered() if isinstance(policy, RegimePriorityPolicy) else (policy.candidate,)


@dataclass(frozen=True)
class BacktestRequest:
    """An immutable backtest request. `start`/`end` are the requested evaluation period (session dates, inclusive).
    `capital` is applied to every instrument independently (never a combined portfolio)."""

    instrument_ids: tuple
    start: date
    end: date
    policy: Policy
    capital: Decimal
    capital_source: CapitalSource = CapitalSource.ACCOUNT_INITIAL_CAPITAL
    cost_model: CostModel = field(default_factory=CostModel)
    warmup_sessions: int = DEFAULT_WARMUP_SESSIONS
    timeframe: str = TIMEFRAME

    def __post_init__(self) -> None:
        bad = RefusalCode.INVALID_CONFIGURATION
        if not isinstance(self.instrument_ids, tuple) or not self.instrument_ids:
            raise BacktestRefused(bad, "instrument_ids must be a non-empty tuple")
        if len(set(self.instrument_ids)) != len(self.instrument_ids):
            raise BacktestRefused(bad, "instrument_ids must be unique")
        for name in ("start", "end"):
            value = getattr(self, name)
            if not isinstance(value, date) or isinstance(value, datetime):
                raise BacktestRefused(RefusalCode.INVALID_PERIOD, f"{name} must be a date")
        if self.end < self.start:
            raise BacktestRefused(RefusalCode.INVALID_PERIOD, f"end {self.end} is before start {self.start}")
        if not isinstance(self.policy, (RegimePriorityPolicy, PinnedPolicy)):
            raise BacktestRefused(bad, "policy must be a RegimePriorityPolicy or PinnedPolicy")
        for candidate in policy_candidates(self.policy):
            problem = configuration_problem(candidate)
            if problem is not None:
                raise BacktestRefused(bad, f"candidate {candidate.config_id}: {problem}")
        try:
            capital = to_decimal("capital", self.capital)
        except ValueError as exc:
            raise BacktestRefused(bad, str(exc)) from None
        if capital <= 0:
            raise BacktestRefused(bad, f"capital must be > 0, got {capital}")
        object.__setattr__(self, "capital", capital)
        if not isinstance(self.capital_source, CapitalSource):
            raise BacktestRefused(bad, "capital_source must be a CapitalSource")
        if not isinstance(self.cost_model, CostModel):
            raise BacktestRefused(bad, "cost_model must be a CostModel")
        w = self.warmup_sessions
        if isinstance(w, bool) or not isinstance(w, int) or not 0 <= w <= MAX_WARMUP_SESSIONS:
            raise BacktestRefused(bad, f"warmup_sessions must be an integer in 0..{MAX_WARMUP_SESSIONS}, got {w!r}")
        if self.timeframe != TIMEFRAME:
            raise BacktestRefused(bad, f"only the {TIMEFRAME} timeframe is supported, got {self.timeframe!r}")

    @property
    def candidates(self) -> tuple[Candidate, ...]:
        return policy_candidates(self.policy)


# ---- results ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DataQuality:
    requested_start: date
    requested_end: date
    effective_start: date
    effective_end: date
    warmup_sessions_requested: int
    warmup_sessions_used: int
    warmup_source: WarmupSource
    warmup_session_dates: tuple[date, ...]
    warmup_bars: int
    warmup_truncated_by_corporate_action: date | None  # a split/bonus ex-date that cut the warm-up history short
    evaluation_sessions: int
    sessions_without_data: tuple[date, ...]
    evaluation_bars: int
    expected_bars: int
    missing_bars: int
    missing_bar_starts: tuple[datetime, ...]
    coverage_pct: Decimal
    bars_ignored_outside_window: int
    corporate_actions_disclosed: tuple[CorporateActionRecord, ...]  # dividends / other in the period (not adjusted)


@dataclass(frozen=True)
class Trade:
    seq: int
    instrument_id: Any
    session_date: date
    config_id: str
    strategy_id: str
    strategy_version: str
    entry_signal_bar_start: datetime
    entry_signal_reason: str
    entry_reference_price: Decimal  # the signal bar's close: a reference, not the simulated price
    entry_bar_start: datetime  # entered at this bar's open
    entry_price: Decimal  # simulated (after slippage)
    quantity: int
    stop_level: Decimal | None
    target_level: Decimal | None
    entry_fees: Decimal
    exit_signal_bar_start: datetime | None  # None for stops and targets
    exit_signal_reason: str | None
    exit_reference_price: Decimal | None
    exit_bar_start: datetime
    exit_timing: ExitTiming
    exit_price: Decimal  # simulated (after slippage)
    exit_reason: ExitReason
    exit_fees: Decimal
    gross_pnl: Decimal  # quantity x (exit price - entry price); slippage is inside the prices, fees are not
    net_pnl: Decimal  # gross minus entry and exit fees


@dataclass(frozen=True)
class Event:
    seq: int
    bar_start: datetime
    session_date: date
    event_type: EventType
    config_id: str | None = None
    detail: Any = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", MappingProxyType(dict(self.detail)))


@dataclass(frozen=True)
class EquityPoint:
    bar_start: datetime
    session_date: date
    cash: Decimal
    quantity: int
    close: Decimal
    equity: Decimal  # cash + quantity x close
    benchmark_equity: Decimal


@dataclass(frozen=True)
class BenchmarkResult:
    status: BenchmarkStatus
    limitations: tuple[str, ...]  # make the comparison LIMITED
    notes: tuple[str, ...]  # always-true caveats
    entry_bar_start: datetime
    entry_price: Decimal
    quantity: int
    entry_fees: Decimal
    last_close: Decimal
    hypothetical_exit_price: Decimal
    hypothetical_exit_fees: Decimal
    final_value: Decimal  # after the hypothetical exit
    return_pct: Decimal


@dataclass(frozen=True)
class InstrumentSummary:
    """Descriptive only: never an input to selection or configuration (decision D15)."""

    starting_capital: Decimal
    final_equity: Decimal
    return_pct: Decimal
    trades: int
    winning_trades: int
    losing_trades: int
    total_fees: Decimal
    max_drawdown_pct: Decimal
    exposure_pct: Decimal
    sessions_with_selection: int
    sessions_without_selection: int
    entry_signals_ignored: int  # entry signals from non-selected candidates or outside the selected window
    benchmark_return_pct: Decimal


@dataclass(frozen=True)
class InstrumentResult:
    instrument_id: Any
    data_quality: DataQuality
    selection_decisions: tuple[Any, ...]  # SelectionDecision.as_dict() per evaluation session
    trades: tuple[Trade, ...]
    events: tuple[Event, ...]
    equity: tuple[EquityPoint, ...]
    benchmark: BenchmarkResult
    summary: InstrumentSummary


@dataclass(frozen=True)
class BacktestResult:
    engine_version: str
    fill_model: str
    sizing_model: str
    price_basis: str
    cost_basis: CostBasis
    capital: Decimal
    capital_source: CapitalSource
    requested_start: date
    requested_end: date
    config_fingerprint: str
    data_fingerprint: str
    result_fingerprint: str
    instruments: tuple[InstrumentResult, ...]
    disclosures: tuple[str, ...]
