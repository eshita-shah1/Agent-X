from datetime import UTC, date, datetime, timedelta

import pytest

from app.marketdata.calendar import EXCHANGE_TZ, TradingCalendar
from app.marketdata.freshness import Thresholds, assess_freshness, assess_provider, worst
from app.marketdata.gaps import compute_coverage, probable_closures
from app.marketdata.types import DataStatus

CAL = TradingCalendar()
TH = Thresholds(timedelta(minutes=5), timedelta(minutes=15), 3)


def starts(day, first=0, last=None):
    return set(CAL.expected_bar_starts(CAL.session(day), "5m")[first:last])


def cov(present, days, until=None):
    sessions = [CAL.session(d) for d in days]
    return compute_coverage(present, sessions, CAL, "5m", until=until)


# ---- coverage / gaps ------------------------------------------------------------------------


def test_complete_day_has_no_missing_intervals():
    r = cov(starts(date(2026, 11, 10)), [date(2026, 11, 10)])
    assert (r.expected_total, r.present_total, r.missing_total) == (75, 75, 0)
    assert r.days[0].status == "COMPLETE" and r.complete_days == [date(2026, 11, 10)]


def test_missing_slots_are_reported_individually_inside_a_present_day():
    present = starts(date(2026, 11, 10)) - set(CAL.expected_bar_starts(CAL.session(date(2026, 11, 10)), "5m")[72:74])
    r = cov(present, [date(2026, 11, 10)])
    d = r.days[0]
    assert d.status == "PARTIAL" and len(d.missing_starts) == 2 and r.missing_total == 2
    assert [m.astimezone(EXCHANGE_TZ).strftime("%H:%M") for m in d.missing_starts] == ["15:15", "15:20"]


def test_a_session_with_no_bars_is_an_unexplained_absence_not_a_holiday():
    r = cov(starts(date(2026, 11, 9)), [date(2026, 11, 9), date(2026, 11, 10)])
    assert r.absent_days == [date(2026, 11, 10)] and r.days[1].status == "ABSENT"
    assert r.missing_total == 75  # reported as missing slots; classification is left to the caller


def test_weekends_are_not_expected_so_they_are_not_gaps():
    sessions = CAL.sessions_between(date(2026, 11, 6), date(2026, 11, 9))  # Fri..Mon, Sat/Sun skipped
    present = starts(date(2026, 11, 6)) | starts(date(2026, 11, 9))
    r = compute_coverage(present, sessions, CAL, "5m")
    assert r.missing_total == 0 and len(r.days) == 2


def test_known_holiday_is_not_expected():
    from app.marketdata.calendar import HOLIDAY, SessionOverride

    cal = TradingCalendar([SessionOverride(date(2026, 11, 10), HOLIDAY, source="t")])
    sessions = cal.sessions_between(date(2026, 11, 9), date(2026, 11, 11))
    present = set(cal.expected_bar_starts(sessions[0], "5m")) | set(cal.expected_bar_starts(sessions[1], "5m"))
    r = compute_coverage(present, sessions, cal, "5m")
    assert r.missing_total == 0 and [d.day for d in r.days] == [date(2026, 11, 9), date(2026, 11, 11)]


def test_future_slots_are_not_counted_as_missing():
    now = datetime(2026, 11, 11, 7, 40, tzinfo=UTC)  # 13:10 IST: bars through 13:05 have ended
    present = set(CAL.expected_bar_starts(CAL.session(date(2026, 11, 11)), "5m")[:47])  # 09:15..13:05
    r = cov(present, [date(2026, 11, 11)], until=now)
    assert r.expected_total == 47 and r.missing_total == 0 and r.days[0].status == "COMPLETE"


def test_unexpected_archived_slots_are_surfaced():
    stray = datetime(2026, 11, 11, 1, 0, tzinfo=UTC)
    r = cov(starts(date(2026, 11, 11)) | {stray}, [date(2026, 11, 11)])
    assert r.unexpected_present == [stray]


def test_probable_closures_need_every_instrument_to_agree():
    d = date(2026, 11, 10)
    assert probable_closures({"a": [d], "b": [d]}, [d]) == [d]
    assert probable_closures({"a": [d], "b": []}, [d]) == []


def test_a_single_instrument_cannot_corroborate_a_closure():
    assert probable_closures({"a": [date(2026, 11, 10)]}, [date(2026, 11, 10)]) == []


# ---- freshness -----------------------------------------------------------------------------


def end_of(h, m, day=11):
    return datetime(2026, 11, day, h, m, tzinfo=EXCHANGE_TZ).astimezone(UTC)


def fresh(latest, now_ist, **kw):
    return assess_freshness(latest_bar_end=latest, now=now_ist, calendar=CAL, timeframe="5m", thresholds=TH)


def test_no_archived_data_is_unavailable_never_healthy():
    f = fresh(None, end_of(11, 0))
    assert f.status is DataStatus.UNAVAILABLE and f.reason == "NO_ARCHIVED_DATA"


def test_up_to_date_is_healthy():
    # now 13:12 IST; margin 5 => bars ended by 13:07 are expected => latest expected end 13:05
    f = fresh(end_of(13, 5), end_of(13, 12))
    assert f.status is DataStatus.HEALTHY and f.lag_seconds == 0


def test_one_bar_behind_is_degraded():
    f = fresh(end_of(13, 0), end_of(13, 12))
    assert f.status is DataStatus.DEGRADED and f.lag_bars == 1.0


def test_beyond_the_stale_threshold_is_stale():
    f = fresh(end_of(12, 30), end_of(13, 12))
    assert f.status is DataStatus.STALE and f.lag_seconds > 15 * 60


def test_closed_market_data_is_not_stale_just_because_time_passed():
    # Friday close archived; it is now Sunday
    f = assess_freshness(latest_bar_end=end_of(15, 30, day=6), now=datetime(2026, 11, 8, 12, 0, tzinfo=UTC), calendar=CAL, timeframe="5m", thresholds=TH)
    assert f.status is DataStatus.HEALTHY


def test_data_age_is_reported_alongside_status():
    f = fresh(end_of(13, 5), end_of(13, 12))
    assert f.data_age_seconds == 7 * 60 and f.latest_bar_end == end_of(13, 5)


def test_worst_status_ordering_and_unknown_handling():
    assert worst(DataStatus.HEALTHY, DataStatus.STALE, DataStatus.DEGRADED) is DataStatus.STALE
    assert worst(DataStatus.INCOMPLETE, DataStatus.DEGRADED) is DataStatus.INCOMPLETE
    assert worst(DataStatus.UNAVAILABLE, DataStatus.STALE) is DataStatus.UNAVAILABLE
    assert worst(DataStatus.UNKNOWN, DataStatus.HEALTHY) is DataStatus.HEALTHY
    assert worst(DataStatus.UNKNOWN) is DataStatus.UNKNOWN


class State:
    def __init__(self, failures=0, attempt=datetime(2026, 11, 11, tzinfo=UTC)):
        self.last_attempt_at, self.last_success_at, self.last_failure_at = attempt, attempt, None
        self.last_error_kind = self.last_error_message = None
        self.consecutive_failures = failures
        self.last_observed_market_ts = self.last_successful_ingestion_at = None


@pytest.mark.parametrize("failures,status", [(0, DataStatus.HEALTHY), (1, DataStatus.DEGRADED), (2, DataStatus.DEGRADED), (3, DataStatus.UNAVAILABLE), (9, DataStatus.UNAVAILABLE)])
def test_provider_status_follows_consecutive_failures(failures, status):
    assert assess_provider(State(failures), TH).status is status


def test_a_never_used_provider_is_unknown_not_healthy():
    assert assess_provider(None, TH).status is DataStatus.UNKNOWN
    assert assess_provider(State(attempt=None), TH).status is DataStatus.UNKNOWN
