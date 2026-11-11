"""Freshness, completeness and provider-health assessment. Pure functions over facts; no I/O.

THRESHOLDS ARE PROVISIONAL PHASE 2 SETTINGS (configurable). They only describe data; they do not block anything
in Phase 2, are not guaranteed provider behaviour and are not trading-risk rules. A future trading engine must
not rely on them to gate entries without a separate review. The observed data age is reported separately.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.marketdata.calendar import TradingCalendar, timeframe_minutes
from app.marketdata.types import DataStatus

_SEVERITY = {
    DataStatus.HEALTHY: 0,
    DataStatus.DEGRADED: 1,
    DataStatus.INCOMPLETE: 2,
    DataStatus.STALE: 3,
    DataStatus.UNAVAILABLE: 4,
    DataStatus.UNKNOWN: -1,
}


def worst(*statuses: DataStatus) -> DataStatus:
    known = [s for s in statuses if s is not DataStatus.UNKNOWN]
    return max(known, key=_SEVERITY.__getitem__) if known else DataStatus.UNKNOWN


@dataclass(frozen=True)
class Thresholds:
    finalization_margin: timedelta
    stale_after: timedelta
    provider_unavailable_after_failures: int


@dataclass
class Freshness:
    status: DataStatus
    reason: str
    latest_bar_end: datetime | None
    expected_latest_bar_end: datetime | None
    data_age_seconds: float | None  # now - latest_bar_end
    lag_seconds: float | None  # expected_latest_bar_end - latest_bar_end (>= 0)
    lag_bars: float | None


def assess_freshness(
    *, latest_bar_end: datetime | None, now: datetime, calendar: TradingCalendar, timeframe: str, thresholds: Thresholds
) -> Freshness:
    """Compare the newest archived bar with the newest bar that *should* be final by now.

    Outside market hours the expectation is the last bar of the last session, so closed-market data is not
    reported stale merely because the clock moved on.
    """
    minutes = timeframe_minutes(timeframe)
    expected = calendar.last_expected_bar_end(now - thresholds.finalization_margin, timeframe)
    if latest_bar_end is None:
        return Freshness(DataStatus.UNAVAILABLE, "NO_ARCHIVED_DATA", None, expected, None, None, None)
    age = (now - latest_bar_end).total_seconds()
    if expected is None:
        return Freshness(DataStatus.UNKNOWN, "NO_SESSION_IN_CALENDAR_WINDOW", latest_bar_end, None, age, None, None)
    lag = max(0.0, (expected - latest_bar_end).total_seconds())
    lag_bars = lag / (minutes * 60)
    if lag == 0:
        status, reason = DataStatus.HEALTHY, "UP_TO_DATE"
    elif lag <= thresholds.stale_after.total_seconds():
        status, reason = DataStatus.DEGRADED, "BEHIND_BUT_WITHIN_STALE_THRESHOLD"
    else:
        status, reason = DataStatus.STALE, "BEHIND_BEYOND_STALE_THRESHOLD"
    return Freshness(status, reason, latest_bar_end, expected, age, lag, round(lag_bars, 2))


@dataclass
class ProviderHealth:
    status: DataStatus
    reason: str
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_error_kind: str | None
    last_error_message: str | None
    consecutive_failures: int
    last_observed_market_ts: datetime | None
    last_successful_ingestion_at: datetime | None


def assess_provider(state, thresholds: Thresholds) -> ProviderHealth:
    """`state` is a ProviderHealthState row (or None when the provider has never been used)."""
    if state is None or state.last_attempt_at is None:
        return ProviderHealth(DataStatus.UNKNOWN, "NO_ATTEMPTS_YET", None, None, None, None, None, 0, None, None)
    failures = state.consecutive_failures
    if failures == 0:
        status, reason = DataStatus.HEALTHY, "LAST_ATTEMPT_SUCCEEDED"
    elif failures >= thresholds.provider_unavailable_after_failures:
        status, reason = DataStatus.UNAVAILABLE, f"{failures}_CONSECUTIVE_FAILURES"
    else:
        status, reason = DataStatus.DEGRADED, f"{failures}_RECENT_FAILURE(S)"
    return ProviderHealth(
        status, reason, state.last_attempt_at, state.last_success_at, state.last_failure_at, state.last_error_kind,
        state.last_error_message, failures, state.last_observed_market_ts, state.last_successful_ingestion_at,
    )
