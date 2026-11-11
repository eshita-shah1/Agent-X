"""Shared helpers for the strategy tests.

`simulate` is a minimal stand-in for the Step 4 engine's position bookkeeping, used only to feed strategies a
realistic PositionContext: an ENTER on bar t opens the position at bar t+1's open (if t+1 is in the same session,
otherwise the entry expires), an EXIT on bar t closes it at bar t+1's open. It executes no stops or targets and
computes no prices. Explicit contexts can be passed instead where a test needs one.
"""

from datetime import date, timedelta

from app.indicators import IndicatorBar
from app.strategies import Action, PositionContext
from tests.indicators import DAY, NEXT_DAY, ist

THIRD_DAY = date(2026, 11, 4)


def slot_of(hhmm: str) -> int:
    hour, minute = map(int, hhmm.split(":"))
    return (hour * 60 + minute - (9 * 60 + 15)) // 5


def at(day: date, hhmm: str, o, h, low, c, volume=1000) -> IndicatorBar:
    """The bar starting at `hhmm` IST on `day`."""
    hour, minute = map(int, hhmm.split(":"))
    return IndicatorBar(ist(day, hour, minute), day, o, h, low, c, volume)


def pbar(day: date, slot: int, close: float, spread: float = 0.5, volume=1000) -> IndicatorBar:
    """A bar whose open equals its close, with high/low `spread` either side."""
    return IndicatorBar(ist(day, 9, 15) + timedelta(minutes=5 * slot), day, close, close + spread, close - spread, close, volume)


def hhmm(signal) -> str:
    from app.marketdata.calendar import EXCHANGE_TZ

    return signal.bar_start.astimezone(EXCHANGE_TZ).strftime("%H:%M")


def simulate(strategy, bars):
    signals, is_open, pending = [], False, None
    prev = None
    for bar in bars:
        closed = False
        if pending is Action.ENTER_LONG and prev is not None and prev.session_date == bar.session_date:
            is_open = True
        elif pending is Action.EXIT_LONG:
            is_open, closed = False, True
        signal = strategy.evaluate(bar, PositionContext(is_open=is_open, closed_this_bar=closed))
        pending = signal.action if signal.action is not Action.NONE else None
        signals.append(signal)
        prev = bar
    return signals


def events(signals):
    """(start time, action, reason) for every non-NONE signal."""
    return [(hhmm(s), s.action, s.reason) for s in signals if s.action is not Action.NONE]


__all__ = ["DAY", "NEXT_DAY", "THIRD_DAY", "at", "events", "hhmm", "pbar", "simulate", "slot_of"]
