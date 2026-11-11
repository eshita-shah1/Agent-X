"""Session-anchored indicators: session VWAP and the opening range. Both reset at each new trading session."""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from datetime import datetime

from app.indicators.core import BarSequence, IndicatorBar, check_period
from app.marketdata.calendar import REGULAR_CLOSE, REGULAR_OPEN, TradingCalendar

BAR_MINUTES = 5  # the indicators run on the archive's 5-minute bars


class SessionVWAP:
    """VWAP from the session's first bar: sum(TP * V) / sum(V), TP = (H + L + C) / 3.

    None while the session's cumulative volume is 0 (Yahoo's 09:15 bar often reports volume 0). This is a VWAP of
    the provider's 5-minute bars, not the exchange's official VWAP."""

    def __init__(self) -> None:
        self._sequence = BarSequence()
        self._price_volume: list[float] = []
        self._volume: list[float] = []

    def update(self, bar: IndicatorBar) -> float | None:
        if self._sequence.accept(bar):
            self._price_volume, self._volume = [], []
        typical = (bar.high + bar.low + bar.close) / 3
        self._price_volume.append(typical * bar.volume)
        self._volume.append(bar.volume)
        volume = math.fsum(self._volume)
        return None if volume == 0 else math.fsum(self._price_volume) / volume


class OpeningRangeStatus(str, enum.Enum):
    FORMING = "FORMING"
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"  # an expected opening-range slot is missing: no range for the whole session


@dataclass(frozen=True)
class OpeningRangeValue:
    status: OpeningRangeStatus
    high: float | None  # set only when COMPLETE
    low: float | None


class OpeningRange:
    """High and low of the session's first `minutes` of bars, from the calendar's expected 5-minute slots.

    All-or-nothing: if any expected slot is absent the session's range is INCOMPLETE and never becomes COMPLETE.
    """

    def __init__(self, minutes: int, calendar: TradingCalendar | None = None) -> None:
        check_period("minutes", minutes)
        regular = (REGULAR_CLOSE.hour * 60 + REGULAR_CLOSE.minute) - (REGULAR_OPEN.hour * 60 + REGULAR_OPEN.minute)
        if minutes % BAR_MINUTES or minutes > regular:
            raise ValueError(f"minutes must be a multiple of {BAR_MINUTES} within the {regular}-minute session, got {minutes!r}")
        self.minutes = minutes
        self.calendar = calendar or TradingCalendar()
        self._sequence = BarSequence()
        self._slots: list[datetime] = []
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._status = OpeningRangeStatus.FORMING

    def _start_session(self, bar: IndicatorBar) -> None:
        session = self.calendar.session(bar.session_date)
        if session is None:
            raise ValueError(f"bar {bar.start_ts.isoformat()} is not in a known trading session")
        self._slots = self.calendar.expected_bar_starts(session, f"{BAR_MINUTES}m")[: self.minutes // BAR_MINUTES]
        self._highs, self._lows = [], []
        # A session too short to contain the whole window can never have a complete range.
        complete_possible = len(self._slots) == self.minutes // BAR_MINUTES
        self._status = OpeningRangeStatus.FORMING if complete_possible else OpeningRangeStatus.INCOMPLETE

    def update(self, bar: IndicatorBar) -> OpeningRangeValue:
        if self._sequence.accept(bar):
            self._start_session(bar)
        if self._status is OpeningRangeStatus.FORMING:
            expected = self._slots[len(self._highs)]
            if bar.start_ts == expected:
                self._highs.append(bar.high)
                self._lows.append(bar.low)
                if len(self._highs) == len(self._slots):
                    self._status = OpeningRangeStatus.COMPLETE
            elif bar.start_ts > expected:
                self._status = OpeningRangeStatus.INCOMPLETE  # the expected slot was skipped: it is missing
            else:
                raise ValueError(f"bar {bar.start_ts.isoformat()} starts before the session's first slot")
        if self._status is OpeningRangeStatus.COMPLETE:
            return OpeningRangeValue(self._status, max(self._highs), min(self._lows))
        return OpeningRangeValue(self._status, None, None)
