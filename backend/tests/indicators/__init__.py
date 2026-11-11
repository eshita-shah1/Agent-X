"""Shared reference data for the indicator tests (approved Step 1 specification, section C).

Expected values are exact rationals (Fraction); comparisons allow only float64 rounding (rel/abs 1e-12).
"""

from datetime import date, datetime, timedelta
from fractions import Fraction

import pytest

from app.indicators import IndicatorBar
from app.marketdata.calendar import EXCHANGE_TZ

F = Fraction
DAY = date(2026, 11, 2)  # a Monday (regular session)
NEXT_DAY = date(2026, 11, 3)

# Dataset V: (open, high, low, close)
V_OHLC = [
    (10, 11, 9, 10),
    (10, 12, 10, 11),
    (11, 13, 11, 12),
    (12, 12, 10, 11),
    (11, 14, 11, 13),
    (13, 15, 13, 14),
    (14, 14, 12, 13),
    (12, 12, 10, 10.5),
]
V_CLOSES = [float(c) for *_, c in V_OHLC]


def ist(day: date, hour: int, minute: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=EXCHANGE_TZ)


def bar(day: date, slot: int, o, h, low, c, volume=1000) -> IndicatorBar:
    """The bar in 5-minute slot `slot` of `day`'s session (slot 0 starts 09:15 IST)."""
    return IndicatorBar(ist(day, 9, 15) + timedelta(minutes=5 * slot), day, o, h, low, c, volume)


def dataset_v() -> list[IndicatorBar]:
    return [bar(DAY, i, *row) for i, row in enumerate(V_OHLC)]


def close_enough(expected):
    return pytest.approx(float(expected), rel=1e-12, abs=1e-12)


def assert_series(actual, expected) -> None:
    """Compare an output series with exact expectations; None means 'not yet defined'."""
    assert len(actual) == len(expected)
    for t, (a, e) in enumerate(zip(actual, expected)):
        if e is None:
            assert a is None, f"t={t}: expected None, got {a!r}"
        else:
            assert a is not None and a == close_enough(e), f"t={t}: expected {float(e)!r}, got {a!r}"
