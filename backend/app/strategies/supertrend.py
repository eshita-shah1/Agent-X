"""Supertrend flip (long-only).

Entry: the Supertrend direction flips DOWN -> UP on this bar (the indicator's `flipped` flag with direction UP).
Exit: it flips UP -> DOWN, or the square-off. No separate stop: the Supertrend line is the close-basis trailing exit.
A flip is an event: one that occurs on a bar where entries are gated (cooldown, cutoff, gap) is not acted on later.
"""

from __future__ import annotations

from typing import Any

from app.indicators.core import IndicatorBar
from app.indicators.trend import Direction, Supertrend
from app.strategies.base import Family, ProtectiveLevels, Reason, Strategy
from app.strategies.params import SupertrendConfig


class SupertrendFlip(Strategy):
    strategy_id = "SUPERTREND"
    family = Family.TREND
    config_type = SupertrendConfig

    def __init__(self, config: SupertrendConfig | None = None, calendar=None) -> None:
        super().__init__(config, calendar)
        self._supertrend = Supertrend(self.config.atr_period, self.config.multiplier)

    def update_indicators(self, bar: IndicatorBar, new_session: bool) -> dict[str, Any]:
        st = self._supertrend.update(bar)
        if st is None:
            return {"close": bar.close, "supertrend_line": None, "direction": None, "flipped": None}
        return {"close": bar.close, "supertrend_line": st.line, "direction": st.direction.value, "flipped": st.flipped}

    def entry(self, bar, values) -> tuple[Reason, ProtectiveLevels | None, bool]:
        if values["direction"] is None:
            return Reason.INDICATOR_NOT_READY, None, False
        if values["flipped"] and values["direction"] == Direction.UP.value:
            return Reason.SUPERTREND_FLIP_UP, None, True
        return Reason.NO_ENTRY_CONDITION, None, False

    def exit_reason(self, bar, values) -> Reason | None:
        if values["flipped"] and values["direction"] == Direction.DOWN.value:
            return Reason.SUPERTREND_FLIP_DOWN
        return None
