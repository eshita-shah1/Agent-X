"""Volatility indicators: True Range, ATR and Bollinger Bands."""

from __future__ import annotations

from dataclasses import dataclass

from app.indicators.core import BarSequence, IndicatorBar, RollingStd, SMA, WilderAverage, check_number, check_period


class TrueRange:
    """TR_t = max(H_t - L_t, |H_t - C_{t-1}|, |L_t - C_{t-1}|). Undefined for the first bar (no previous close)."""

    def __init__(self) -> None:
        self._sequence = BarSequence()
        self._prev_close: float | None = None

    def update(self, bar: IndicatorBar) -> float | None:
        self._sequence.accept(bar)
        prev, self._prev_close = self._prev_close, bar.close
        if prev is None:
            return None
        return max(bar.high - bar.low, abs(bar.high - prev), abs(bar.low - prev))


class ATR:
    """Average True Range: Wilder(n) over TR, so the first value is mean(TR_1..TR_n), at t = n."""

    def __init__(self, period: int) -> None:
        self.period = check_period("period", period)
        self._tr = TrueRange()
        self._avg = WilderAverage(self.period)

    def update(self, bar: IndicatorBar) -> float | None:
        tr = self._tr.update(bar)
        return None if tr is None else self._avg.update(tr)


@dataclass(frozen=True)
class BollingerValue:
    mid: float
    upper: float
    lower: float


class Bollinger:
    """Bollinger Bands over closes: mid = SMA(n), bands = mid +/- k * population stddev(n). Defined from t = n - 1."""

    def __init__(self, period: int, k: float) -> None:
        self.period = check_period("period", period)
        self.k = check_number("k", k)
        if self.k < 0:
            raise ValueError(f"k must be >= 0, got {k!r}")
        self._mid = SMA(self.period)
        self._std = RollingStd(self.period)

    def update(self, close: float) -> BollingerValue | None:
        mid, std = self._mid.update(close), self._std.update(close)
        if mid is None or std is None:
            return None
        return BollingerValue(mid, mid + self.k * std, mid - self.k * std)
