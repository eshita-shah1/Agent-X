"""Momentum indicators."""

from __future__ import annotations

from app.indicators.core import WilderAverage, check_number, check_period


class RSI:
    """Relative Strength Index over closes, with Wilder smoothing of gains and losses.

    delta_t = C_t - C_{t-1}; gain = max(delta, 0), loss = max(-delta, 0); AG, AL = Wilder(n) of each.
    RSI = 100 - 100 / (1 + AG/AL). AG = AL = 0 -> 50 (flat); AL = 0 < AG -> 100. Defined from t = n.
    """

    def __init__(self, period: int) -> None:
        self.period = check_period("period", period)
        self._gain = WilderAverage(self.period)
        self._loss = WilderAverage(self.period)
        self._prev: float | None = None

    def update(self, close: float) -> float | None:
        c = check_number("close", close)
        prev, self._prev = self._prev, c
        if prev is None:
            return None
        delta = c - prev
        avg_gain = self._gain.update(max(delta, 0.0))
        avg_loss = self._loss.update(max(-delta, 0.0))
        if avg_gain is None or avg_loss is None:
            return None
        if avg_loss == 0:
            return 50.0 if avg_gain == 0 else 100.0
        return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
