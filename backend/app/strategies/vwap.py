"""VWAP cross (long-only).

Entry: from the bar ending at `eligible_from` (09:30), the close crosses above the session VWAP:
C_{t-1} <= VWAP_{t-1} and C_t > VWAP_t, both bars in the same session. Protective stop: `stop_atr_multiple` x
ATR(atr_period) below the entry fill. Exit: the close crosses back below the VWAP (C_{t-1} >= VWAP_{t-1} and
C_t < VWAP_t), the stop (backtest), or the square-off.

Data caveat: the VWAP is computed from Yahoo 5-minute bars. Their volume understates exchange volume and the 09:15
bar often reports 0, so this is an approximation of the official VWAP; while a session's cumulative volume is 0 the
VWAP (and therefore any signal) is unavailable.
"""

from __future__ import annotations

from typing import Any

from app.indicators.core import IndicatorBar
from app.indicators.session import SessionVWAP
from app.indicators.volatility import ATR
from app.strategies.base import Family, ProtectiveLevels, Reason, Strategy
from app.strategies.params import VWAPConfig


class VWAPCross(Strategy):
    strategy_id = "VWAP"
    family = Family.TREND
    config_type = VWAPConfig

    def __init__(self, config: VWAPConfig | None = None, calendar=None) -> None:
        super().__init__(config, calendar)
        self._vwap = SessionVWAP()
        self._atr = ATR(self.config.atr_period)
        self._prev_close: float | None = None
        self._prev_vwap: float | None = None

    def update_indicators(self, bar: IndicatorBar, new_session: bool) -> dict[str, Any]:
        if new_session:
            self._prev_close = self._prev_vwap = None  # the VWAP resets: no cross against the previous session
        vwap, atr = self._vwap.update(bar), self._atr.update(bar)
        values = {"close": bar.close, "vwap": vwap, "prev_close": self._prev_close, "prev_vwap": self._prev_vwap, "atr": atr}
        self._prev_close, self._prev_vwap = bar.close, vwap
        return values

    def eligible_from(self):
        return self.config.eligible_from

    def entry(self, bar, values) -> tuple[Reason, ProtectiveLevels | None, bool]:
        if any(values[k] is None for k in ("vwap", "prev_vwap", "atr")):
            return Reason.INDICATOR_NOT_READY, None, False
        if not (values["prev_close"] <= values["prev_vwap"] and values["close"] > values["vwap"]):
            return Reason.NO_ENTRY_CONDITION, None, False
        stop = self.config.stop_atr_multiple * values["atr"]
        if stop <= 0:
            return Reason.DEGENERATE_PROTECTIVE_LEVEL, None, False
        return Reason.VWAP_CROSS_UP, ProtectiveLevels(stop_distance=stop), True

    def exit_reason(self, bar, values) -> Reason | None:
        if values["vwap"] is None or values["prev_vwap"] is None:
            return None
        if values["prev_close"] >= values["prev_vwap"] and values["close"] < values["vwap"]:
            return Reason.VWAP_CROSS_DOWN
        return None
