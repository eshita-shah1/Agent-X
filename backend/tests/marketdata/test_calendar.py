from datetime import UTC, date, datetime, time, timedelta

import pytest

from app.marketdata.calendar import (
    EXCHANGE_TZ,
    HOLIDAY,
    SPECIAL_SESSION,
    SessionOverride,
    TradingCalendar,
    timeframe_minutes,
)

CAL = TradingCalendar()
WED = date(2026, 11, 11)


def test_regular_session_is_0915_to_1530_ist_expressed_in_utc():
    s = CAL.session(WED)
    assert s.open_ts == datetime(2026, 11, 11, 3, 45, tzinfo=UTC)  # 09:15 IST
    assert s.close_ts == datetime(2026, 11, 11, 10, 0, tzinfo=UTC)  # 15:30 IST
    assert s.open_ts.astimezone(EXCHANGE_TZ).strftime("%H:%M") == "09:15"


def test_weekends_have_no_session():
    assert CAL.session(date(2026, 11, 14)) is None  # Saturday
    assert CAL.session(date(2026, 11, 15)) is None  # Sunday


def test_a_regular_day_has_75_five_minute_bars_first_0915_last_1525():
    bars = CAL.expected_bar_starts(CAL.session(WED), "5m")
    assert len(bars) == 75
    assert bars[0].astimezone(EXCHANGE_TZ).strftime("%H:%M") == "09:15"
    assert bars[-1].astimezone(EXCHANGE_TZ).strftime("%H:%M") == "15:25"


def test_bar_counts_for_other_timeframes():
    s = CAL.session(WED)
    assert len(CAL.expected_bar_starts(s, "15m")) == 25
    assert len(CAL.expected_bar_starts(s, "1m")) == 375


def test_timeframe_minutes_rejects_daily_and_unknown():
    assert timeframe_minutes("5m") == 5
    for bad in ("1d", "2m", "x"):
        with pytest.raises(ValueError):
            timeframe_minutes(bad)


def test_no_holidays_are_invented():
    assert CAL.known_overrides == 0
    assert "no holiday source" in CAL.source
    assert not CAL.is_known_holiday(date(2026, 10, 2))  # a weekday that may well be a holiday: not asserted


def test_holiday_override_removes_the_session():
    cal = TradingCalendar([SessionOverride(WED, HOLIDAY, source="test")])
    assert cal.session(WED) is None and cal.is_known_holiday(WED)
    assert cal.session(date(2026, 11, 10)) is not None


def test_special_session_override_sets_custom_hours():
    cal = TradingCalendar([SessionOverride(date(2026, 11, 14), SPECIAL_SESSION, time(9, 15), time(11, 15), "test")])  # a Saturday
    s = cal.session(date(2026, 11, 14))
    assert s is not None and s.kind == SPECIAL_SESSION
    assert len(cal.expected_bar_starts(s, "5m")) == 24


def test_invalid_special_session_override_is_rejected():
    cal = TradingCalendar([SessionOverride(WED, SPECIAL_SESSION)])
    with pytest.raises(ValueError):
        cal.session(WED)


@pytest.mark.parametrize(
    "hh,mm,inside",
    [(9, 15, True), (9, 10, False), (15, 25, True), (15, 30, False), (15, 20, True), (8, 0, False), (18, 15, False)],
)
def test_bar_is_in_session_boundaries(hh, mm, inside):
    ts = datetime(2026, 11, 11, hh, mm, tzinfo=EXCHANGE_TZ).astimezone(UTC)
    assert CAL.bar_is_in_session(ts, "5m") is inside


def test_weekend_bar_is_out_of_session():
    assert not CAL.bar_is_in_session(datetime(2026, 11, 14, 10, 0, tzinfo=EXCHANGE_TZ).astimezone(UTC), "5m")


def test_sessions_between_is_inclusive_and_skips_weekends():
    days = [s.day for s in CAL.sessions_between(date(2026, 11, 6), date(2026, 11, 10))]
    assert days == [date(2026, 11, 6), date(2026, 11, 9), date(2026, 11, 10)]


def test_last_expected_bar_end_during_session():
    now = datetime(2026, 11, 11, 7, 37, tzinfo=UTC)  # 13:07 IST
    end = CAL.last_expected_bar_end(now, "5m")
    assert end.astimezone(EXCHANGE_TZ).strftime("%H:%M") == "13:05"


def test_last_expected_bar_end_after_close_is_the_final_bar_of_that_day():
    end = CAL.last_expected_bar_end(datetime(2026, 11, 11, 14, 0, tzinfo=UTC), "5m")
    assert end.astimezone(EXCHANGE_TZ).strftime("%Y-%m-%d %H:%M") == "2026-11-11 15:30"


def test_last_expected_bar_end_on_weekend_is_fridays_close():
    end = CAL.last_expected_bar_end(datetime(2026, 11, 14, 8, 0, tzinfo=UTC), "5m")  # Saturday
    assert end.astimezone(EXCHANGE_TZ).strftime("%Y-%m-%d %H:%M") == "2026-11-13 15:30"


def test_last_expected_bar_end_before_open_looks_back_to_previous_session():
    end = CAL.last_expected_bar_end(datetime(2026, 11, 11, 3, 0, tzinfo=UTC), "5m")  # 08:30 IST
    assert end.astimezone(EXCHANGE_TZ).strftime("%Y-%m-%d %H:%M") == "2026-11-10 15:30"


def test_naive_local_date_conversion_uses_exchange_timezone():
    # 20:00 UTC on 10 Nov is already 01:30 IST on 11 Nov
    assert CAL.local_date(datetime(2026, 11, 10, 20, 0, tzinfo=UTC)) == date(2026, 11, 11)
