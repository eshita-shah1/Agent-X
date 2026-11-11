"""Gap and coverage computation against the trading calendar. Pure functions: no I/O.

Terminology (kept deliberately separate):
  * missing interval   - an expected bar inside a known session that is absent from the archive.
  * partial day        - a session day with some, but not all, expected bars.
  * unexplained absence- a session day (per the calendar) with *no* bars at all. With no authoritative
                         holiday source this may be a holiday or a data gap; it is never silently one or the other.
  * probable closure   - an unexplained absence on which every instrument under comparison also had no data
                         while neighbouring days had data. This is an inference, labelled as such.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app.marketdata.calendar import Session, TradingCalendar, timeframe_minutes


@dataclass
class DayCoverage:
    day: date
    expected: int
    present: int
    missing_starts: list[datetime] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.present == 0:
            return "ABSENT"
        return "COMPLETE" if not self.missing_starts else "PARTIAL"


@dataclass
class CoverageResult:
    expected_total: int
    present_total: int
    missing_total: int
    days: list[DayCoverage]
    unexpected_present: list[datetime]  # archived bars that are not on any expected slot

    @property
    def absent_days(self) -> list[date]:
        return [d.day for d in self.days if d.status == "ABSENT"]

    @property
    def partial_days(self) -> list[DayCoverage]:
        return [d for d in self.days if d.status == "PARTIAL"]

    @property
    def complete_days(self) -> list[date]:
        return [d.day for d in self.days if d.status == "COMPLETE"]


def compute_coverage(
    present_starts: Iterable[datetime],
    sessions: list[Session],
    calendar: TradingCalendar,
    timeframe: str,
    until: datetime | None = None,
) -> CoverageResult:
    """`until`: slots whose bar has not yet ended by then are not expected (they lie in the future)."""
    present = set(present_starts)
    days: list[DayCoverage] = []
    expected_all: set[datetime] = set()
    expected_total = present_total = 0
    step = timedelta(minutes=timeframe_minutes(timeframe))
    for session in sessions:
        expected = calendar.expected_bar_starts(session, timeframe)
        if until is not None:
            expected = [t for t in expected if t + step <= until]
        expected_all.update(expected)
        got = [t for t in expected if t in present]
        missing = [t for t in expected if t not in present]
        days.append(DayCoverage(session.day, len(expected), len(got), missing))
        expected_total += len(expected)
        present_total += len(got)
    return CoverageResult(
        expected_total=expected_total,
        present_total=present_total,
        missing_total=expected_total - present_total,
        days=days,
        unexpected_present=sorted(present - expected_all),
    )


def probable_closures(absent_days_by_instrument: Mapping[str, Iterable[date]], all_session_days: Iterable[date]) -> list[date]:
    """Days absent for *every* instrument although other session days had data: likely market closures.

    This is an inference from the data and must be labelled as such; it is not a calendar.
    """
    sets = [set(v) for v in absent_days_by_instrument.values()]
    if len(sets) < 2:
        return []  # one instrument cannot corroborate itself
    common = set.intersection(*sets)
    return sorted(d for d in common if d in set(all_session_days))
