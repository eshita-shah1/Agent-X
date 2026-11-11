"""EMA crossover (long-only).

Entry: EMA(fast) crosses above EMA(slow): fast_{t-1} <= slow_{t-1} and fast_t > slow_t. Protective stop:
`stop_atr_multiple` x ATR(atr_period) below the entry fill. Exit: EMA(fast) crosses below EMA(slow)
(fast_{t-1} >= slow_{t-1} and fast_t < slow_t), the stop (backtest), or the square-off.
The EMAs run continuously across sessions (decision D13).
"""

from __future__ import annotations

from typing import Any

from app.indicators.core import EMA, IndicatorBar
from app.indicators.volatility import ATR
from app.strategies.base import Family, ProtectiveLevels, Reason, Strategy
from app.strategies.params import EMACrossoverConfig


class EMACrossover(Strategy):
    strategy_id = "EMA_CROSSOVER"
    family = Family.TREND
    config_type = EMACrossoverConfig

    def __init__(self, config: EMACrossoverConfig | None = None, calendar=None) -> None:
        super().__init__(config, calendar)
        self._fast = EMA(self.config.fast_period)
        self._slow = EMA(self.config.slow_period)
        self._atr = ATR(self.config.atr_period)
        self._prev_fast: float | None = None
        self._prev_slow: float | None = None

    def update_indicators(self, bar: IndicatorBar, new_session: bool) -> dict[str, Any]:
        fast, slow, atr = self._fast.update(bar.close), self._slow.update(bar.close), self._atr.update(bar)
        values = {"close": bar.close, "ema_fast": fast, "ema_slow": slow, "prev_ema_fast": self._prev_fast, "prev_ema_slow": self._prev_slow, "atr": atr}
        self._prev_fast, self._prev_slow = fast, slow
        return values

    def entry(self, bar, values) -> tuple[Reason, ProtectiveLevels | None, bool]:
        if any(values[k] is None for k in ("ema_fast", "ema_slow", "prev_ema_fast", "prev_ema_slow", "atr")):
            return Reason.INDICATOR_NOT_READY, None, False
        if not (values["prev_ema_fast"] <= values["prev_ema_slow"] and values["ema_fast"] > values["ema_slow"]):
            return Reason.NO_ENTRY_CONDITION, None, False
        stop = self.config.stop_atr_multiple * values["atr"]
        if stop <= 0:
            return Reason.DEGENERATE_PROTECTIVE_LEVEL, None, False
        return Reason.EMA_CROSS_UP, ProtectiveLevels(stop_distance=stop), True

    def exit_reason(self, bar, values) -> Reason | None:
        if any(values[k] is None for k in ("ema_fast", "ema_slow", "prev_ema_fast", "prev_ema_slow")):
            return None
        if values["prev_ema_fast"] >= values["prev_ema_slow"] and values["ema_fast"] < values["ema_slow"]:
            return Reason.EMA_CROSS_DOWN
        return None
