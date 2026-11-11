"""Provider-neutral interfaces.

A single adapter may implement several of them. An adapter must implement an interface only if the
capability is genuinely supported; unverified capabilities are declared with their true status
(DOCUMENTED / UNKNOWN) and the corresponding methods raise UnsupportedCapability when unusable.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from app.marketdata.types import (
    Capability,
    CapabilityStatus,
    ProviderBarsResult,
    ProviderInstrumentMetadata,
    ProviderQuote,
    RawCorporateAction,
)


@dataclass(frozen=True)
class DeclaredCapability:
    capability: Capability
    status: CapabilityStatus
    note: str = ""


@dataclass(frozen=True)
class ProviderDescriptor:
    code: str
    name: str
    declared: tuple[DeclaredCapability, ...] = field(default_factory=tuple)
    notes: str = ""
    # interval -> how far back a request may start. Only intervals with a *measured* limit appear here; the
    # service refuses to ingest any other interval. `lookback_safety` keeps a request start safely inside the
    # limit (the provider evaluates the window at its own, slightly later, "now").
    max_lookback_by_interval: Mapping[str, timedelta] = field(default_factory=dict)
    lookback_safety: timedelta = timedelta(minutes=10)
    # Measured provider behaviour: when a request starts inside a trading session the FIRST returned bar is
    # built only from data after the start (truncated open/volume/etc.). The pipeline discards that bar.
    truncates_first_bar_when_request_starts_inside_session: bool = False


@runtime_checkable
class HistoricalDataProvider(Protocol):
    descriptor: ProviderDescriptor

    def fetch_history(
        self, provider_symbol: str, interval: str, start: datetime, end: datetime
    ) -> ProviderBarsResult:
        """Candles for [start, end). Interval is one of the project's timeframes ('5m', '1d', ...)."""
        ...

    def fetch_corporate_actions(self, provider_symbol: str) -> list[RawCorporateAction]:
        ...


@runtime_checkable
class IntradayDataProvider(Protocol):
    descriptor: ProviderDescriptor

    def fetch_latest_intraday(self, provider_symbol: str, interval: str, lookback: timedelta) -> ProviderBarsResult:
        """The most recent `lookback` of intraday candles, ending now. May include an unfinished final bar."""
        ...


@runtime_checkable
class QuoteDataProvider(Protocol):
    descriptor: ProviderDescriptor

    def fetch_quote(self, provider_symbol: str) -> ProviderQuote:
        ...


@runtime_checkable
class InstrumentMetadataProvider(Protocol):
    descriptor: ProviderDescriptor

    def fetch_metadata(self, provider_symbol: str) -> ProviderInstrumentMetadata:
        ...
