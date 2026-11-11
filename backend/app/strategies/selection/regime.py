"""Market-regime classification from the approved DMI/ADX indicator (decision D8).

TREND when ADX >= 25, RANGE when ADX <= 20, UNDETERMINED in between or whenever ADX is unavailable or invalid.
The classifier only reads an ADXValue; the formulas live in app.indicators.trend. The tracker feeds ADX(14)
continuously across sessions (decision D13) and is read at the 09:30 IST checkpoint by the session selector.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from datetime import datetime, time

from app.indicators.core import IndicatorBar, check_number, check_period
from app.indicators.trend import ADX, ADXValue
from app.strategies.base import BAR, check_session_time


class Regime(str, enum.Enum):
    TREND = "TREND"
    RANGE = "RANGE"
    UNDETERMINED = "UNDETERMINED"


class RegimeReason(str, enum.Enum):
    ADX_AT_OR_ABOVE_TREND_THRESHOLD = "ADX_AT_OR_ABOVE_TREND_THRESHOLD"
    ADX_AT_OR_BELOW_RANGE_THRESHOLD = "ADX_AT_OR_BELOW_RANGE_THRESHOLD"
    ADX_BETWEEN_THRESHOLDS = "ADX_BETWEEN_THRESHOLDS"
    ADX_NOT_READY = "ADX_NOT_READY"  # no bars yet, or ADX still warming up (needs 2n bars)
    INVALID_INDICATOR_VALUE = "INVALID_INDICATOR_VALUE"


@dataclass(frozen=True)
class RegimeConfig:
    """Approved values: ADX(14), TREND at >= 25, RANGE at <= 20, checkpoint 09:30 IST."""

    adx_period: int = 14
    trend_min: float = 25.0
    range_max: float = 20.0
    checkpoint: time = time(9, 30)

    def __post_init__(self) -> None:
        check_period("adx_period", self.adx_period)
        trend_min, range_max = check_number("trend_min", self.trend_min), check_number("range_max", self.range_max)
        if not 0 <= range_max < trend_min <= 100:
            raise ValueError("thresholds must satisfy 0 <= range_max < trend_min <= 100")
        check_session_time("checkpoint", self.checkpoint)


@dataclass(frozen=True)
class RegimeResult:
    regime: Regime
    reason: RegimeReason
    checkpoint: datetime  # the session's checkpoint instant (tz-aware)
    adx: float | None
    plus_di: float | None
    minus_di: float | None
    input_bar_start: datetime | None  # the latest bar the classification used (it ends at or before the checkpoint)
    input_bar_end: datetime | None
    bars_used: int  # all bars observed up to the checkpoint (ADX runs across sessions)
    session_bars_used: int  # of which in this session
    adx_period: int
    trend_min: float
    range_max: float


def _finite_di(value: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 100


def classify(value: ADXValue | None, config: RegimeConfig) -> tuple[Regime, RegimeReason]:
    """Pure threshold rule. A missing or invalid value is UNDETERMINED, never guessed."""
    if value is None or value.adx is None:
        return Regime.UNDETERMINED, RegimeReason.ADX_NOT_READY
    if not (_finite_di(value.adx) and _finite_di(value.plus_di) and _finite_di(value.minus_di)):
        return Regime.UNDETERMINED, RegimeReason.INVALID_INDICATOR_VALUE
    if value.adx >= config.trend_min:
        return Regime.TREND, RegimeReason.ADX_AT_OR_ABOVE_TREND_THRESHOLD
    if value.adx <= config.range_max:
        return Regime.RANGE, RegimeReason.ADX_AT_OR_BELOW_RANGE_THRESHOLD
    return Regime.UNDETERMINED, RegimeReason.ADX_BETWEEN_THRESHOLDS


class RegimeTracker:
    """Feeds every bar of one instrument to ADX(n) and remembers the latest value and the bar it came from.

    The caller must snapshot before feeding any bar that ends after the checkpoint (the session selector does)."""

    def __init__(self, config: RegimeConfig) -> None:
        self.config = config
        self._adx = ADX(config.adx_period)
        self._value: ADXValue | None = None
        self._last: IndicatorBar | None = None
        self.bars_observed = 0

    def update(self, bar: IndicatorBar) -> None:
        self._value = self._adx.update(bar)
        self._last = bar
        self.bars_observed += 1

    def snapshot(self, checkpoint: datetime, session_bars_used: int) -> RegimeResult:
        regime, reason = classify(self._value, self.config)
        v, last = self._value, self._last
        if last is not None and last.start_ts + BAR > checkpoint:
            raise ValueError("snapshot after a bar that ends past the checkpoint would use future information")
        return RegimeResult(
            regime=regime,
            reason=reason,
            checkpoint=checkpoint,
            adx=None if v is None else v.adx,
            plus_di=None if v is None else v.plus_di,
            minus_di=None if v is None else v.minus_di,
            input_bar_start=None if last is None else last.start_ts,
            input_bar_end=None if last is None else last.start_ts + BAR,
            bars_used=self.bars_observed,
            session_bars_used=session_bars_used,
            adx_period=self.config.adx_period,
            trend_min=self.config.trend_min,
            range_max=self.config.range_max,
        )
