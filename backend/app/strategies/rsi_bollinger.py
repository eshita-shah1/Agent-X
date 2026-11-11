"""RSI + Bollinger Bands mean reversion (long-only).

Entry: the close is below the lower Bollinger band AND RSI(rsi_period) is below `oversold`
(C_t < lower_t and RSI_t < oversold, both strict). Protective stop: `stop_atr_multiple` x ATR(atr_period) below the
entry fill. Exit: the close reverts to the middle band (C_t >= mid_t), the stop (backtest), or the square-off.
`overbought` is validated but not used while the strategy is long-only (reserved for the deferred short side).
"""

from __future__ import annotations

from typing import Any

from app.indicators.core import IndicatorBar
from app.indicators.momentum import RSI
from app.indicators.volatility import ATR, Bollinger
from app.strategies.base import Family, ProtectiveLevels, Reason, Strategy
from app.strategies.params import RSIBollingerConfig


class RSIBollingerReversion(Strategy):
    strategy_id = "RSI_BOLLINGER"
    family = Family.MEAN_REVERSION
    config_type = RSIBollingerConfig

    def __init__(self, config: RSIBollingerConfig | None = None, calendar=None) -> None:
        super().__init__(config, calendar)
        self._rsi = RSI(self.config.rsi_period)
        self._bands = Bollinger(self.config.bollinger_period, self.config.bollinger_k)
        self._atr = ATR(self.config.atr_period)

    def update_indicators(self, bar: IndicatorBar, new_session: bool) -> dict[str, Any]:
        rsi, bands, atr = self._rsi.update(bar.close), self._bands.update(bar.close), self._atr.update(bar)
        return {
            "close": bar.close,
            "rsi": rsi,
            "bb_mid": None if bands is None else bands.mid,
            "bb_upper": None if bands is None else bands.upper,
            "bb_lower": None if bands is None else bands.lower,
            "atr": atr,
        }

    def entry(self, bar, values) -> tuple[Reason, ProtectiveLevels | None, bool]:
        if any(values[k] is None for k in ("rsi", "bb_lower", "atr")):
            return Reason.INDICATOR_NOT_READY, None, False
        if not (values["close"] < values["bb_lower"] and values["rsi"] < self.config.oversold):
            return Reason.NO_ENTRY_CONDITION, None, False
        stop = self.config.stop_atr_multiple * values["atr"]
        if stop <= 0:
            return Reason.DEGENERATE_PROTECTIVE_LEVEL, None, False
        return Reason.OVERSOLD_BELOW_LOWER_BAND, ProtectiveLevels(stop_distance=stop), True

    def exit_reason(self, bar, values) -> Reason | None:
        if values["bb_mid"] is not None and values["close"] >= values["bb_mid"]:
            return Reason.REVERTED_TO_MIDDLE_BAND
        return None
