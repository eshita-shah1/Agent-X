"""A single source of "now" so time-dependent rules (freshness, session dates) are testable."""

from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)
