"""Deterministic test doubles for market data. Nothing here touches the network or yfinance."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.marketdata.calendar import EXCHANGE_TZ, TradingCalendar
from app.marketdata.errors import NoDataReturned, ProviderError, ProviderUnavailable
from app.marketdata.providers.base import DeclaredCapability, ProviderDescriptor
from app.marketdata.types import (
    Capability,
    CapabilityStatus,
    ProviderBarsResult,
    ProviderInstrumentMetadata,
    ProviderQuote,
    RawBar,
    RawCorporateAction,
)

CAL = TradingCalendar()
# 2026-11-11 is a Wednesday. 11:00 UTC = 16:30 IST, i.e. after that day's close.
NOW = datetime(2026, 11, 11, 11, 0, tzinfo=UTC)
WEEKDAYS = [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11)]


def day_bars(day: date, price: float = 100.0, *, first: int = 0, last: int | None = None, volume: float = 1000.0) -> list[RawBar]:
    """Valid 5m bars (aware IST timestamps) for one regular session; slots [first:last)."""
    session = CAL.session(day)
    starts = CAL.expected_bar_starts(session, "5m")[first:last]
    bars = []
    for i, s in enumerate(starts):
        p = round(price + (first + i) * 0.05, 2)
        bars.append(RawBar(s.astimezone(EXCHANGE_TZ), p, p + 0.5, p - 0.5, p + 0.25, volume + first + i))  # absolute slot index
    return bars


def days_bars(days: list[date], **kw) -> list[RawBar]:
    out: list[RawBar] = []
    for d in days:
        out.extend(day_bars(d, **kw))
    return out


class FakeProvider:
    """Serves a scripted bar list. Implements all four provider interfaces; records every call."""

    def __init__(self, bars: list[RawBar] | None = None, clock: Callable[[], datetime] | None = None, code: str = "yahoo", truncates_first_bar: bool = True):
        self.bars = list(bars or [])
        self.clock = clock or (lambda: NOW)
        self.actions: list[RawCorporateAction] = []
        self.actions_error: ProviderError | None = None
        self.fail_with: ProviderError | None = None
        self.fail_symbols: dict[str, ProviderError] = {}
        self.calls: list[tuple] = []
        self.descriptor = ProviderDescriptor(
            code=code,
            name="Fake provider",
            declared=(
                DeclaredCapability(Capability.HISTORICAL_INTRADAY_CANDLES, CapabilityStatus.DOCUMENTED),
                DeclaredCapability(Capability.FIVE_MINUTE_INTERVAL, CapabilityStatus.DOCUMENTED),
                DeclaredCapability(Capability.CORPORATE_ACTIONS, CapabilityStatus.DOCUMENTED),
                DeclaredCapability(Capability.QUOTE_RETRIEVAL, CapabilityStatus.UNKNOWN),
            ),
            max_lookback_by_interval={"5m": timedelta(days=60)},
            lookback_safety=timedelta(minutes=10),
            truncates_first_bar_when_request_starts_inside_session=truncates_first_bar,  # mirrors measured Yahoo behaviour
        )

    def fetch_history(self, provider_symbol, interval, start, end) -> ProviderBarsResult:
        self.calls.append(("history", provider_symbol, interval, start, end))
        if provider_symbol in self.fail_symbols:
            raise self.fail_symbols[provider_symbol]
        if self.fail_with:
            raise self.fail_with
        rows = [b for b in self.bars if start <= b.start_ts < end]
        if not rows:
            raise NoDataReturned("no rows in window", provider=self.descriptor.code, symbol=provider_symbol)
        return ProviderBarsResult(
            provider_code=self.descriptor.code, provider_symbol=provider_symbol, interval=interval, request_start=start,
            request_end=end, bars=rows, observed_at=self.clock(), reported_timezone="Asia/Kolkata",
            meta={"library": "fake", "library_version": "0", "params": {"auto_adjust": False}},
        )

    def fetch_latest_intraday(self, provider_symbol, interval, lookback):
        now = self.clock()
        return self.fetch_history(provider_symbol, interval, now - lookback, now)

    def fetch_corporate_actions(self, provider_symbol):
        self.calls.append(("actions", provider_symbol))
        if self.actions_error:
            raise self.actions_error
        return list(self.actions)

    def fetch_quote(self, provider_symbol):
        return ProviderQuote(provider_symbol, None, "INR", self.clock(), None)

    def fetch_metadata(self, provider_symbol):
        return ProviderInstrumentMetadata(provider_symbol, "NSI", "INR", "Asia/Kolkata", None)

    @property
    def history_calls(self):
        return [c for c in self.calls if c[0] == "history"]


@dataclass
class FakeClock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)
