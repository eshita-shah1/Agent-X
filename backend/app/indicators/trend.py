"""Trend indicators: DMI/ADX and Supertrend."""

from __future__ import annotations

import enum
from dataclasses import dataclass

from app.indicators.core import IndicatorBar, WilderAverage, check_number, check_period
from app.indicators.volatility import ATR, TrueRange


@dataclass(frozen=True)
class ADXValue:
    plus_di: float
    minus_di: float
    dx: float
    adx: float | None  # defined from t = 2n - 1; DI and DX from t = n


class ADX:
    """Wilder's Directional Movement Index and Average Directional Index.

    up = H_t - H_{t-1}, down = L_{t-1} - L_t; +DM = up if up > down and up > 0 else 0; -DM = down if down > up and
    down > 0 else 0 (a tie gives both 0). +/-DI = 100 * Wilder(n)(+/-DM) / Wilder(n)(TR), 0 when Wilder(TR) = 0.
    DX = 100 * |+DI - -DI| / (+DI + -DI), 0 when the sum is 0. ADX = Wilder(n)(DX).
    """

    def __init__(self, period: int) -> None:
        self.period = check_period("period", period)
        self._tr = TrueRange()  # also enforces bar order
        self._avg_tr = WilderAverage(self.period)
        self._avg_plus = WilderAverage(self.period)
        self._avg_minus = WilderAverage(self.period)
        self._avg_dx = WilderAverage(self.period)
        self._prev: IndicatorBar | None = None

    def update(self, bar: IndicatorBar) -> ADXValue | None:
        tr = self._tr.update(bar)
        prev, self._prev = self._prev, bar
        if prev is None or tr is None:
            return None
        up, down = bar.high - prev.high, prev.low - bar.low
        plus_dm = up if (up > down and up > 0) else 0.0
        minus_dm = down if (down > up and down > 0) else 0.0
        avg_tr = self._avg_tr.update(tr)
        avg_plus = self._avg_plus.update(plus_dm)
        avg_minus = self._avg_minus.update(minus_dm)
        if avg_tr is None or avg_plus is None or avg_minus is None:
            return None
        plus_di = 0.0 if avg_tr == 0 else 100.0 * avg_plus / avg_tr
        minus_di = 0.0 if avg_tr == 0 else 100.0 * avg_minus / avg_tr
        di_sum = plus_di + minus_di
        dx = 0.0 if di_sum == 0 else 100.0 * abs(plus_di - minus_di) / di_sum
        return ADXValue(plus_di, minus_di, dx, self._avg_dx.update(dx))


class Direction(str, enum.Enum):
    UP = "UP"
    DOWN = "DOWN"


@dataclass(frozen=True)
class SupertrendValue:
    basic_upper: float
    basic_lower: float
    upper: float  # final upper band
    lower: float  # final lower band
    line: float  # the lower band in an uptrend, the upper band in a downtrend
    direction: Direction
    flipped: bool  # direction changed on this bar (never true on the initialization bar)


class Supertrend:
    """Supertrend(n, m) on ATR(n).

    mid = (H + L) / 2; BU/BL = mid +/- m * ATR. On the first defined bar FU = BU, FL = BL and the direction is DOWN
    (approved convention). Then FU_t = BU_t if BU_t < FU_{t-1} or C_{t-1} > FU_{t-1}, else FU_{t-1};
    FL_t = BL_t if BL_t > FL_{t-1} or C_{t-1} < FL_{t-1}, else FL_{t-1}. The direction flips DOWN -> UP when
    C_t > FU_t and UP -> DOWN when C_t < FL_t (both strict).
    """

    def __init__(self, period: int, multiplier: float) -> None:
        self.period = check_period("period", period)
        self.multiplier = check_number("multiplier", multiplier)
        if self.multiplier <= 0:
            raise ValueError(f"multiplier must be > 0, got {multiplier!r}")
        self._atr = ATR(self.period)
        self._prev_close: float | None = None
        self._upper: float | None = None
        self._lower: float | None = None
        self._direction: Direction | None = None

    def update(self, bar: IndicatorBar) -> SupertrendValue | None:
        atr = self._atr.update(bar)
        prev_close, self._prev_close = self._prev_close, bar.close
        if atr is None:
            return None
        mid = (bar.high + bar.low) / 2
        basic_upper, basic_lower = mid + self.multiplier * atr, mid - self.multiplier * atr
        if self._direction is None:
            self._upper, self._lower, self._direction = basic_upper, basic_lower, Direction.DOWN
            flipped = False
        else:
            if basic_upper < self._upper or prev_close > self._upper:
                self._upper = basic_upper
            if basic_lower > self._lower or prev_close < self._lower:
                self._lower = basic_lower
            previous = self._direction
            if previous is Direction.DOWN and bar.close > self._upper:
                self._direction = Direction.UP
            elif previous is Direction.UP and bar.close < self._lower:
                self._direction = Direction.DOWN
            flipped = self._direction is not previous
        line = self._lower if self._direction is Direction.UP else self._upper
        return SupertrendValue(basic_upper, basic_lower, self._upper, self._lower, line, self._direction, flipped)
