"""Strategy interface and the rules every strategy shares (approved Phase 3 decisions).

A strategy is a deterministic, long-only signal generator. The caller (the backtest engine, Step 4) feeds it every
finalized 5-minute bar in time order together with the state of the position the strategy holds; the strategy
updates its indicators and returns exactly one Signal for that bar. It never fills orders, sizes positions,
executes stops or targets, or sees a bar after the one it is evaluating.

Decision order for a bar (the first rule that applies wins, so every Signal carries one unambiguous reason):

  position open:  session square-off (bar end >= square_off) -> the strategy's own exit condition -> hold
  flat:           cooldown -> before the strategy's eligible time -> after the entry cutoff -> gap after a missing
                  expected bar -> the strategy's own entry rules (warm-up, eligibility, condition)

Times are compared on the bar's END in exchange time (IST): a signal is decided at the close of its bar, and the
earliest fill (in the backtest) is the open of the next bar.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from types import MappingProxyType
from typing import Any, ClassVar

from app.indicators.core import BarSequence, IndicatorBar, check_number
from app.marketdata.calendar import EXCHANGE_TZ, TradingCalendar

BAR = timedelta(minutes=5)  # strategies run on the archive's 5-minute bars
TIMEFRAME = "5m"


class Action(str, enum.Enum):
    ENTER_LONG = "ENTER_LONG"
    EXIT_LONG = "EXIT_LONG"
    NONE = "NONE"


class Family(str, enum.Enum):
    """Static strategy metadata, used by the Step 3 selector."""

    OPENING_BREAKOUT = "OPENING_BREAKOUT"
    TREND = "TREND"
    MEAN_REVERSION = "MEAN_REVERSION"


class Reason(str, enum.Enum):
    # entries
    ORB_BREAKOUT = "ORB_BREAKOUT"
    VWAP_CROSS_UP = "VWAP_CROSS_UP"
    EMA_CROSS_UP = "EMA_CROSS_UP"
    OVERSOLD_BELOW_LOWER_BAND = "OVERSOLD_BELOW_LOWER_BAND"
    SUPERTREND_FLIP_UP = "SUPERTREND_FLIP_UP"
    # exits
    SESSION_SQUARE_OFF = "SESSION_SQUARE_OFF"
    VWAP_CROSS_DOWN = "VWAP_CROSS_DOWN"
    EMA_CROSS_DOWN = "EMA_CROSS_DOWN"
    REVERTED_TO_MIDDLE_BAND = "REVERTED_TO_MIDDLE_BAND"
    SUPERTREND_FLIP_DOWN = "SUPERTREND_FLIP_DOWN"
    # no signal
    POSITION_OPEN_NO_EXIT = "POSITION_OPEN_NO_EXIT"
    COOLDOWN = "COOLDOWN"
    BEFORE_ELIGIBLE_TIME = "BEFORE_ELIGIBLE_TIME"
    AFTER_ENTRY_CUTOFF = "AFTER_ENTRY_CUTOFF"
    GAP_SUPPRESSED = "GAP_SUPPRESSED"
    INDICATOR_NOT_READY = "INDICATOR_NOT_READY"
    OPENING_RANGE_FORMING = "OPENING_RANGE_FORMING"
    OPENING_RANGE_INCOMPLETE = "OPENING_RANGE_INCOMPLETE"
    SESSION_ENTRY_LIMIT = "SESSION_ENTRY_LIMIT"
    DEGENERATE_PROTECTIVE_LEVEL = "DEGENERATE_PROTECTIVE_LEVEL"
    NO_ENTRY_CONDITION = "NO_ENTRY_CONDITION"


@dataclass(frozen=True)
class PositionContext:
    """What the caller tells the strategy about the position it holds for this instrument.

    `closed_this_bar`: a position held by this strategy was closed at this bar's open or during this bar (by an
    exit signal, a stop, a target or the square-off). It starts the cooldown."""

    is_open: bool = False
    closed_this_bar: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.is_open, bool) or not isinstance(self.closed_this_bar, bool):
            raise ValueError("is_open and closed_this_bar must be booleans")
        if self.is_open and self.closed_this_bar:
            raise ValueError("a position cannot be open and closed on the same bar (entries wait for the cooldown)")


FLAT = PositionContext()


@dataclass(frozen=True)
class ProtectiveLevels:
    """Stop/target instructions attached to an entry. The backtest (Step 4) executes them; nothing here does.

    stop_price: an absolute price below which the position is stopped (ORB: the opening-range low).
    stop_distance / target_distance: offsets below / above the simulated entry price, which is only known at fill."""

    stop_price: float | None = None
    stop_distance: float | None = None
    target_distance: float | None = None

    def __post_init__(self) -> None:
        if self.stop_price is not None and self.stop_distance is not None:
            raise ValueError("give either stop_price or stop_distance, not both")
        for name in ("stop_price", "stop_distance", "target_distance"):
            value = getattr(self, name)
            if value is not None and check_number(name, value) <= 0:
                raise ValueError(f"{name} must be > 0, got {value!r}")


@dataclass(frozen=True)
class Signal:
    strategy_id: str
    strategy_version: str
    bar_start: datetime  # the bar whose close the decision was made on
    action: Action
    reason: Reason
    reference_price: float  # that bar's close: a reference for the decision, never a fill price
    protective: ProtectiveLevels | None = None  # set on ENTER_LONG only
    values: Any = field(default_factory=dict)  # read-only snapshot of the indicator values behind the decision

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        if self.protective is not None and self.action is not Action.ENTER_LONG:
            raise ValueError("protective levels belong to entries only")


def check_session_time(name: str, value: Any) -> time:
    if not isinstance(value, time) or value.tzinfo is not None:
        raise ValueError(f"{name} must be a naive datetime.time in exchange time, got {value!r}")
    if value.second or value.microsecond or value.minute % 5:
        raise ValueError(f"{name} must lie on the 5-minute grid, got {value}")
    if not time(9, 15) <= value <= time(15, 30):
        raise ValueError(f"{name} must lie within the regular session 09:15-15:30, got {value}")
    return value


@dataclass(frozen=True)
class StrategyConfig:
    """Rules shared by every strategy (approved decisions D5 and the one-bar cooldown). Immutable."""

    entry_cutoff: time = time(14, 45)  # no entry signal on a bar ending after this time
    square_off: time = time(15, 15)  # an open position is exited at the close of the first bar ending at/after this
    cooldown_bars: int = 1  # bars after a position closes during which no entry signal is given

    def __post_init__(self) -> None:
        check_session_time("entry_cutoff", self.entry_cutoff)
        check_session_time("square_off", self.square_off)
        if self.entry_cutoff > self.square_off:
            raise ValueError("entry_cutoff must not be later than square_off")
        if isinstance(self.cooldown_bars, bool) or not isinstance(self.cooldown_bars, int) or self.cooldown_bars < 0:
            raise ValueError(f"cooldown_bars must be an integer >= 0, got {self.cooldown_bars!r}")


class SessionTracker:
    """Session boundaries and missing-bar detection from the calendar's expected 5-minute slots (no filling)."""

    def __init__(self, calendar: TradingCalendar) -> None:
        self.calendar = calendar
        self._sequence = BarSequence()
        self._slots: dict[datetime, int] = {}
        self._prev_index = -1

    def observe(self, bar: IndicatorBar) -> tuple[bool, bool]:
        """Returns (new_session, gap): gap means at least one expected bar before this one is missing."""
        new_session = self._sequence.accept(bar)
        if new_session:
            session = self.calendar.session(bar.session_date)
            if session is None:
                raise ValueError(f"bar {bar.start_ts.isoformat()} is not in a known trading session")
            self._slots = {start: i for i, start in enumerate(self.calendar.expected_bar_starts(session, TIMEFRAME))}
            self._prev_index = -1
        index = self._slots.get(bar.start_ts)
        if index is None:
            raise ValueError(f"bar {bar.start_ts.isoformat()} is not an expected 5-minute slot of its session")
        gap = index != self._prev_index + 1
        self._prev_index = index
        return new_session, gap


def bar_end_local(bar: IndicatorBar) -> time:
    return (bar.start_ts + BAR).astimezone(EXCHANGE_TZ).time()


class Strategy:
    """Base class: applies the common rules; subclasses supply indicators, entry rules and exit rules."""

    strategy_id: ClassVar[str]
    version: ClassVar[str] = "1"
    family: ClassVar[Family]
    config_type: ClassVar[type[StrategyConfig]] = StrategyConfig

    def __init__(self, config: StrategyConfig | None = None, calendar: TradingCalendar | None = None) -> None:
        config = self.config_type() if config is None else config
        if type(config) is not self.config_type:
            raise ValueError(f"{self.strategy_id} needs a {self.config_type.__name__}, got {type(config).__name__}")
        self.config = config
        self.calendar = calendar or TradingCalendar()
        self._session = SessionTracker(self.calendar)
        self._bars_since_close: int | None = None

    # ---- subclass hooks ------------------------------------------------------------------------------------------

    def update_indicators(self, bar: IndicatorBar, new_session: bool) -> dict[str, Any]:
        """Feed the bar to every indicator (always, open position or not) and return the values snapshot."""
        raise NotImplementedError

    def entry(self, bar: IndicatorBar, values: dict[str, Any]) -> tuple[Reason, ProtectiveLevels | None, bool]:
        """(reason, protective levels, enter?) once the common gates have passed."""
        raise NotImplementedError

    def exit_reason(self, bar: IndicatorBar, values: dict[str, Any]) -> Reason | None:
        return None

    def eligible_from(self) -> time | None:
        return None

    def on_entry_signal(self) -> None:
        pass

    # ---- the common decision -------------------------------------------------------------------------------------

    def evaluate(self, bar: IndicatorBar, position: PositionContext) -> Signal:
        if not isinstance(position, PositionContext):
            raise ValueError(f"position must be a PositionContext, got {type(position).__name__}")
        new_session, gap = self._session.observe(bar)
        if new_session and position.is_open:
            raise ValueError("a position must never be carried into a new session (square-off is mandatory)")
        values = self.update_indicators(bar, new_session)
        if position.closed_this_bar:
            self._bars_since_close = 0
        elif self._bars_since_close is not None:
            self._bars_since_close += 1

        end = bar_end_local(bar)
        if position.is_open:
            if end >= self.config.square_off:
                return self._signal(bar, Action.EXIT_LONG, Reason.SESSION_SQUARE_OFF, values)
            reason = self.exit_reason(bar, values)
            if reason is not None:
                return self._signal(bar, Action.EXIT_LONG, reason, values)
            return self._signal(bar, Action.NONE, Reason.POSITION_OPEN_NO_EXIT, values)

        if self._bars_since_close is not None and self._bars_since_close < self.config.cooldown_bars:
            return self._signal(bar, Action.NONE, Reason.COOLDOWN, values)
        eligible = self.eligible_from()
        if eligible is not None and end < eligible:
            return self._signal(bar, Action.NONE, Reason.BEFORE_ELIGIBLE_TIME, values)
        if end > self.config.entry_cutoff:
            return self._signal(bar, Action.NONE, Reason.AFTER_ENTRY_CUTOFF, values)
        if gap:
            return self._signal(bar, Action.NONE, Reason.GAP_SUPPRESSED, values)
        reason, protective, enter = self.entry(bar, values)
        if not enter:
            return self._signal(bar, Action.NONE, reason, values)
        self.on_entry_signal()
        return self._signal(bar, Action.ENTER_LONG, reason, values, protective)

    def _signal(self, bar, action, reason, values, protective=None) -> Signal:
        return Signal(self.strategy_id, self.version, bar.start_ts, action, reason, bar.close, protective, values)
