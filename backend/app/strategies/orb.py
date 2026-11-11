"""Opening Range Breakout (long-only).

Entry: the session's opening range (first `range_minutes`, from the calendar's expected slots) is COMPLETE, the
bar's close is above the range high, and no ORB entry has been signalled yet this session. Protective levels:
stop at the range low (absolute), target `target_multiple` x range height above the entry fill.
An INCOMPLETE range (any expected slot missing) never trades: missing bars are not inferred.
Exits: stop and target are executed by the backtest; the strategy itself exits only at the session square-off.
"""

from __future__ import annotations

from typing import Any

from app.indicators.core import IndicatorBar
from app.indicators.session import OpeningRange, OpeningRangeStatus
from app.strategies.base import Family, ProtectiveLevels, Reason, Strategy
from app.strategies.params import ORBConfig


class OpeningRangeBreakout(Strategy):
    strategy_id = "ORB"
    family = Family.OPENING_BREAKOUT
    config_type = ORBConfig

    def __init__(self, config: ORBConfig | None = None, calendar=None) -> None:
        super().__init__(config, calendar)
        self._range = OpeningRange(self.config.range_minutes, self.calendar)
        self._entries_this_session = 0

    def update_indicators(self, bar: IndicatorBar, new_session: bool) -> dict[str, Any]:
        if new_session:
            self._entries_this_session = 0
        value = self._range.update(bar)
        return {"close": bar.close, "or_status": value.status.value, "or_high": value.high, "or_low": value.low}

    def entry(self, bar, values) -> tuple[Reason, ProtectiveLevels | None, bool]:
        if self._entries_this_session >= 1:
            return Reason.SESSION_ENTRY_LIMIT, None, False
        status = OpeningRangeStatus(values["or_status"])
        if status is OpeningRangeStatus.FORMING:
            return Reason.OPENING_RANGE_FORMING, None, False
        if status is OpeningRangeStatus.INCOMPLETE:
            return Reason.OPENING_RANGE_INCOMPLETE, None, False
        high, low = values["or_high"], values["or_low"]
        if not bar.close > high:
            return Reason.NO_ENTRY_CONDITION, None, False
        height = high - low
        if height <= 0:
            return Reason.DEGENERATE_PROTECTIVE_LEVEL, None, False  # a zero-height range has no meaningful target
        return Reason.ORB_BREAKOUT, ProtectiveLevels(stop_price=low, target_distance=self.config.target_multiple * height), True

    def on_entry_signal(self) -> None:
        self._entries_this_session += 1
