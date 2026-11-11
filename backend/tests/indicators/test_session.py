"""Session VWAP and opening range: reference vectors, resets and the all-or-nothing rule."""

from datetime import date, time

import pytest

from app.indicators import OpeningRange, OpeningRangeStatus, OpeningRangeValue, SessionVWAP, run
from app.marketdata.calendar import SPECIAL_SESSION, SessionOverride, TradingCalendar
from tests.indicators import DAY, NEXT_DAY, F, assert_series, bar, ist

FORMING, COMPLETE, INCOMPLETE = OpeningRangeStatus.FORMING, OpeningRangeStatus.COMPLETE, OpeningRangeStatus.INCOMPLETE

# ---- session VWAP -------------------------------------------------------------------------------------------------------


def vwap_bars():
    return [
        bar(DAY, 0, 100, 101, 99, 100, 0),
        bar(DAY, 1, 101, 102, 100, 101, 100),
        bar(DAY, 2, 101, 103, 100, 102, 300),
        bar(DAY, 3, 102, 102, 99, 99, 100),
        bar(NEXT_DAY, 0, 110, 111, 109, 110, 50),
    ]


def test_vwap_reference_vector_with_zero_volume_first_bar_and_reset():
    assert_series(run(SessionVWAP(), vwap_bars()), [None, 101, F(203, 2), F(506, 5), 110])


def test_vwap_stays_undefined_for_a_session_without_volume():
    bars = [bar(DAY, i, 100, 101, 99, 100, 0) for i in range(4)]
    assert run(SessionVWAP(), bars) == [None] * 4


def test_vwap_of_a_new_session_ignores_the_previous_session():
    full = run(SessionVWAP(), vwap_bars())
    alone = run(SessionVWAP(), vwap_bars()[4:])
    assert full[4] == alone[0] == 110.0


def test_vwap_rejects_out_of_order_bars():
    v = SessionVWAP()
    v.update(bar(DAY, 1, 100, 101, 99, 100))
    with pytest.raises(ValueError):
        v.update(bar(DAY, 0, 100, 101, 99, 100))


# ---- opening range ---------------------------------------------------------------------------------------------------------


def or_bars(day=DAY, slots=(0, 1, 2, 3)):
    rows = {0: (100, 101, 99, 100), 1: (101, 103, 100, 102), 2: (102, 102, 98, 99), 3: (100, 105, 100, 104)}
    return [bar(day, s, *rows[s]) for s in slots]


def test_opening_range_reference_vector():
    out = run(OpeningRange(15), or_bars())
    assert [v.status for v in out] == [FORMING, FORMING, COMPLETE, COMPLETE]
    assert out[0] == OpeningRangeValue(FORMING, None, None)
    assert (out[2].high, out[2].low) == (103.0, 98.0) and (out[3].high, out[3].low) == (103.0, 98.0)  # 09:30 bar ignored


@pytest.mark.parametrize("missing", [0, 1, 2])
def test_any_missing_slot_makes_the_whole_session_incomplete(missing):
    slots = tuple(s for s in (0, 1, 2, 3) if s != missing)
    out = run(OpeningRange(15), or_bars(slots=slots))
    assert out[-1].status is INCOMPLETE and all(v.status is not COMPLETE for v in out)
    assert all(v.high is None and v.low is None for v in out)


def test_incomplete_never_recovers_within_the_session():
    bars = or_bars(slots=(0, 2, 3)) + [bar(DAY, s, 100, 101, 99, 100) for s in range(4, 20)]
    assert {v.status for v in run(OpeningRange(15), bars)[1:]} == {INCOMPLETE}


def test_a_new_session_starts_forming_again():
    bars = or_bars(slots=(0, 2)) + or_bars(NEXT_DAY)
    out = run(OpeningRange(15), bars)
    assert [v.status for v in out] == [FORMING, INCOMPLETE, FORMING, FORMING, COMPLETE, COMPLETE]


def test_a_session_that_ends_before_the_window_completes_never_becomes_complete():
    out = run(OpeningRange(15), or_bars(slots=(0, 1)) + or_bars(NEXT_DAY, slots=(0,)))
    assert [v.status for v in out] == [FORMING, FORMING, FORMING]


def test_window_length_follows_the_minutes_parameter():
    out = run(OpeningRange(5), or_bars())
    assert out[0] == OpeningRangeValue(COMPLETE, 101.0, 99.0)
    out = run(OpeningRange(20), or_bars())
    assert [v.status for v in out] == [FORMING, FORMING, FORMING, COMPLETE] and (out[3].high, out[3].low) == (105.0, 98.0)


def test_slots_come_from_the_injected_trading_calendar():
    special = date(2026, 11, 8)  # a Sunday with a special session 18:00-18:10 (two 5-minute slots)
    cal = TradingCalendar([SessionOverride(special, SPECIAL_SESSION, time(18, 0), time(18, 10), "test")])
    from app.indicators import IndicatorBar

    bars = [IndicatorBar(ist(special, 18, 0), special, 10, 11, 9, 10, 5), IndicatorBar(ist(special, 18, 5), special, 10, 12, 9, 11, 5)]
    assert run(OpeningRange(10, cal), bars)[-1] == OpeningRangeValue(COMPLETE, 12.0, 9.0)
    assert {v.status for v in run(OpeningRange(15, cal), bars)} == {INCOMPLETE}  # session shorter than the window


def test_a_bar_outside_any_session_is_rejected():
    saturday = date(2026, 11, 7)
    from app.indicators import IndicatorBar

    with pytest.raises(ValueError, match="trading session"):
        OpeningRange(15).update(IndicatorBar(ist(saturday, 9, 15), saturday, 10, 11, 9, 10, 1))


def test_a_bar_before_the_first_slot_is_rejected():
    from app.indicators import IndicatorBar

    with pytest.raises(ValueError, match="before the session"):
        OpeningRange(15).update(IndicatorBar(ist(DAY, 9, 10), DAY, 10, 11, 9, 10, 1))


@pytest.mark.parametrize("bad", [0, 7, 380, -5, 15.0, True])
def test_invalid_opening_range_minutes(bad):
    with pytest.raises(ValueError):
        OpeningRange(bad)
