"""NSE trading-calendar model.

What is and is not known (documented in docs/MARKET_DATA.md):
  * KNOWN: the regular equity session is 09:15-15:30 IST, Monday to Friday.
  * NOT INTEGRATED: an authoritative exchange-holiday / special-session list (O13: no source approved yet).
    This module therefore ships with NO holidays. Holidays and special sessions can only enter through
    explicit `SessionOverride` entries (backed by the `exchange_calendar_days` table). Until an approved
    source is loaded, a weekday without data is reported as an *unexplained absence*, never silently
    treated as either a holiday or a gap.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

EXCHANGE_TZ = ZoneInfo("Asia/Kolkata")
REGULAR_OPEN = time(9, 15)
REGULAR_CLOSE = time(15, 30)

HOLIDAY = "HOLIDAY"
SPECIAL_SESSION = "SPECIAL_SESSION"


@dataclass(frozen=True)
class SessionOverride:
    day: date
    kind: str  # HOLIDAY | SPECIAL_SESSION
    open: time | None = None  # for SPECIAL_SESSION
    close: time | None = None
    source: str = ""


@dataclass(frozen=True)
class Session:
    day: date
    open_ts: datetime  # tz-aware UTC
    close_ts: datetime
    kind: str  # "REGULAR" | "SPECIAL_SESSION"


def timeframe_minutes(timeframe: str) -> int:
    table = {"1m": 1, "5m": 5, "15m": 15, "60m": 60}
    if timeframe not in table:
        raise ValueError(f"no fixed-minute bar length for timeframe {timeframe!r}")
    return table[timeframe]


class TradingCalendar:
    source = "weekday-regular-session; no holiday source approved or integrated (O13: no authoritative NSE calendar)"

    def __init__(self, overrides: Iterable[SessionOverride] | Mapping[date, SessionOverride] = ()):
        if isinstance(overrides, Mapping):
            overrides = overrides.values()
        self._overrides: dict[date, SessionOverride] = {o.day: o for o in overrides}

    @property
    def known_overrides(self) -> int:
        return len(self._overrides)

    def to_local(self, ts: datetime) -> datetime:
        return ts.astimezone(EXCHANGE_TZ)

    def local_date(self, ts: datetime) -> date:
        return self.to_local(ts).date()

    def session(self, day: date) -> Session | None:
        """The session for a local date, or None when the exchange is known (or presumed) closed."""
        override = self._overrides.get(day)
        if override is not None:
            if override.kind == HOLIDAY:
                return None
            if override.kind == SPECIAL_SESSION and override.open and override.close:
                return self._build(day, override.open, override.close, SPECIAL_SESSION)
            raise ValueError(f"invalid override for {day}: {override}")
        if day.weekday() >= 5:
            return None
        return self._build(day, REGULAR_OPEN, REGULAR_CLOSE, "REGULAR")

    @staticmethod
    def _build(day: date, open_t: time, close_t: time, kind: str) -> Session:
        o = datetime.combine(day, open_t, tzinfo=EXCHANGE_TZ).astimezone(UTC)
        c = datetime.combine(day, close_t, tzinfo=EXCHANGE_TZ).astimezone(UTC)
        return Session(day, o, c, kind)

    def is_known_holiday(self, day: date) -> bool:
        o = self._overrides.get(day)
        return o is not None and o.kind == HOLIDAY

    def sessions_between(self, start: date, end: date) -> list[Session]:
        """Sessions for local dates start..end inclusive."""
        out, d = [], start
        while d <= end:
            s = self.session(d)
            if s:
                out.append(s)
            d += timedelta(days=1)
        return out

    def expected_bar_starts(self, session: Session, timeframe: str) -> list[datetime]:
        step = timedelta(minutes=timeframe_minutes(timeframe))
        out, t = [], session.open_ts
        while t + step <= session.close_ts:
            out.append(t)
            t += step
        return out

    def bar_is_in_session(self, bar_start: datetime, timeframe: str) -> bool:
        """True when the whole bar lies inside a known session."""
        session = self.session(self.local_date(bar_start))
        if session is None:
            return False
        step = timedelta(minutes=timeframe_minutes(timeframe))
        return session.open_ts <= bar_start and bar_start + step <= session.close_ts

    def last_expected_bar_end(self, now: datetime, timeframe: str) -> datetime | None:
        """End of the latest bar whose scheduled end is at or before `now` (searching back up to 10 days)."""
        step = timedelta(minutes=timeframe_minutes(timeframe))
        day = self.local_date(now)
        for _ in range(11):
            s = self.session(day)
            if s is not None:
                ends = [t + step for t in self.expected_bar_starts(s, timeframe) if t + step <= now]
                if ends:
                    return ends[-1]
            day -= timedelta(days=1)
        return None
