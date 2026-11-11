"""Shared indicator building blocks: the bar type, input validation and the scalar primitives.

Conventions (approved Step 1 specification):
  * float64 throughout; window sums use math.fsum (exactly rounded, so results never depend on summation order);
  * an indicator returns None until it is defined, never NaN;
  * `run()` is a plain fold over `update()`, so batch and incremental results are the same object of computation.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from app.marketdata.calendar import EXCHANGE_TZ


def check_period(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be an integer >= 1, got {value!r}")
    return value


def check_number(name: str, value: Any) -> float:
    """A finite real number (bool is rejected: it is an int subclass, but never a valid price or parameter)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a real number, got {value!r}")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return value


@dataclass(frozen=True)
class IndicatorBar:
    """One finalized candle as the indicators see it. `session_date` is the exchange-local (IST) trading date."""

    start_ts: datetime
    session_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if not isinstance(self.start_ts, datetime) or self.start_ts.tzinfo is None:
            raise ValueError(f"start_ts must be a timezone-aware datetime, got {self.start_ts!r}")
        if self.start_ts.astimezone(EXCHANGE_TZ).date() != self.session_date:
            raise ValueError(f"session_date {self.session_date} does not match start_ts {self.start_ts.isoformat()}")
        for name in ("open", "high", "low", "close"):
            v = check_number(name, getattr(self, name))
            if v <= 0:
                raise ValueError(f"{name} must be > 0, got {v!r}")
            object.__setattr__(self, name, v)
        volume = check_number("volume", self.volume)
        if volume < 0:
            raise ValueError(f"volume must be >= 0, got {volume!r}")
        object.__setattr__(self, "volume", volume)
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError(f"inconsistent OHLC: {self.open}/{self.high}/{self.low}/{self.close}")


class BarSequence:
    """Rejects anything but IndicatorBars in strictly increasing time order (and never-decreasing sessions)."""

    def __init__(self) -> None:
        self._last: IndicatorBar | None = None

    def accept(self, bar: IndicatorBar) -> bool:
        """Validate `bar`; returns True when it starts a new session (including the very first bar)."""
        if not isinstance(bar, IndicatorBar):
            raise ValueError(f"expected an IndicatorBar, got {type(bar).__name__}")
        last = self._last
        if last is not None and bar.start_ts <= last.start_ts:
            raise ValueError(f"bars must be strictly increasing in time: {bar.start_ts.isoformat()} after {last.start_ts.isoformat()}")
        self._last = bar
        return last is None or bar.session_date != last.session_date


class SMA:
    """Simple moving average: fsum of the last n values / n. Defined from the n-th value."""

    def __init__(self, period: int) -> None:
        self.period = check_period("period", period)
        self._window: deque[float] = deque(maxlen=self.period)

    def update(self, value: float) -> float | None:
        self._window.append(check_number("value", value))
        if len(self._window) < self.period:
            return None
        return math.fsum(self._window) / self.period


class RollingStd:
    """Population standard deviation (divide by n) of the last n values, computed two-pass over the window."""

    def __init__(self, period: int) -> None:
        self.period = check_period("period", period)
        self._window: deque[float] = deque(maxlen=self.period)

    def update(self, value: float) -> float | None:
        self._window.append(check_number("value", value))
        if len(self._window) < self.period:
            return None
        mean = math.fsum(self._window) / self.period
        return math.sqrt(math.fsum((x - mean) ** 2 for x in self._window) / self.period)


class EMA:
    """Exponential moving average, alpha = 2/(n+1), seeded with the SMA of the first n values."""

    def __init__(self, period: int) -> None:
        self.period = check_period("period", period)
        self.alpha = 2.0 / (self.period + 1)
        self._seed: list[float] = []
        self._value: float | None = None

    def update(self, value: float) -> float | None:
        x = check_number("value", value)
        if self._value is None:
            self._seed.append(x)
            if len(self._seed) < self.period:
                return None
            self._value = math.fsum(self._seed) / self.period
        else:
            self._value = self.alpha * x + (1 - self.alpha) * self._value
        return self._value


class WilderAverage:
    """Wilder smoothing: the mean of the first n values, then S_t = (S_{t-1} * (n - 1) + x_t) / n.

    Callers feed it only once their own input is defined, so a late-starting series is handled naturally."""

    def __init__(self, period: int) -> None:
        self.period = check_period("period", period)
        self._seed: list[float] = []
        self._value: float | None = None

    def update(self, value: float) -> float | None:
        x = check_number("value", value)
        if self._value is None:
            self._seed.append(x)
            if len(self._seed) < self.period:
                return None
            self._value = math.fsum(self._seed) / self.period
        else:
            self._value = (self._value * (self.period - 1) + x) / self.period
        return self._value


def run(indicator: Any, inputs: Iterable[Any]) -> list[Any]:
    """Feed `inputs` to `indicator.update` in order and collect the outputs (batch == incremental by construction)."""
    return [indicator.update(x) for x in inputs]
