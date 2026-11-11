"""Yahoo Finance adapter (via `yfinance`, an unofficial wrapper; no SLA, coverage and accuracy not guaranteed).

All Yahoo/pandas specifics live here. Every request sets the adjustment-related parameters explicitly:
yfinance's own defaults have changed between versions (`auto_adjust` now defaults to True).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any


from app.marketdata.errors import (
    InvalidRequest,
    MalformedResponse,
    NoDataReturned,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
    UnsupportedCapability,
)
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

PROVIDER_CODE = "yahoo"

# Explicit, never defaulted. `raise_errors=True` makes failures raise instead of returning an empty frame;
# `keepna=True` stops yfinance silently dropping NaN rows so the validation layer can see and report them.
HISTORY_PARAMS: dict[str, Any] = {
    "prepost": False,
    "actions": True,
    "auto_adjust": False,
    "back_adjust": False,
    "repair": False,
    "keepna": True,
    "rounding": False,
    "raise_errors": True,
}

SUPPORTED_INTERVALS = {"1m": "1m", "5m": "5m", "15m": "15m", "60m": "60m", "1d": "1d"}
REQUIRED_COLUMNS = ("Open", "High", "Low", "Close", "Volume")

DESCRIPTOR = ProviderDescriptor(
    code=PROVIDER_CODE,
    name="Yahoo Finance (yfinance)",
    notes="Unofficial access; availability, latency and accuracy are not guaranteed.",
    # Measured 2026-11-11 (probe + edge test): a 5m request starting 60 days minus 1 minute back succeeds;
    # exactly 60 days is rejected ("must be within the last 60 days"). Other intervals are not measured.
    max_lookback_by_interval={"5m": timedelta(days=60)},
    lookback_safety=timedelta(minutes=10),
    # Measured 2026-11-11 against a 59-day reference: a request starting at/before the 09:15 open returned
    # bars identical to the reference; any start inside the session (even exactly on a bar boundary) returned a
    # first bar with different open/volume (e.g. volume 76,930 vs 156,911).
    truncates_first_bar_when_request_starts_inside_session=True,
    declared=(
        DeclaredCapability(Capability.HISTORICAL_DAILY_CANDLES, CapabilityStatus.DOCUMENTED),
        DeclaredCapability(
            Capability.HISTORICAL_INTRADAY_CANDLES,
            CapabilityStatus.DOCUMENTED,
            "yfinance documents intraday history as limited to roughly the last 60 days",
        ),
        DeclaredCapability(Capability.FIVE_MINUTE_INTERVAL, CapabilityStatus.DOCUMENTED),
        DeclaredCapability(Capability.LATEST_CANDLE, CapabilityStatus.UNKNOWN),
        DeclaredCapability(Capability.CORPORATE_ACTIONS, CapabilityStatus.DOCUMENTED),
        DeclaredCapability(Capability.QUOTE_RETRIEVAL, CapabilityStatus.UNKNOWN),
        DeclaredCapability(Capability.INSTRUMENT_METADATA, CapabilityStatus.UNKNOWN),
        DeclaredCapability(Capability.ADJUSTED_INTRADAY_PRICES, CapabilityStatus.UNKNOWN),
        DeclaredCapability(
            Capability.FUTURES_CONTRACT_DATA,
            CapabilityStatus.UNKNOWN,
            "Not probed: futures are disabled and no futures symbol mapping exists",
        ),
    ),
)


def _yf():
    import yfinance

    return yfinance


def _translate(exc: Exception, symbol: str) -> ProviderError:
    """Map yfinance/network failures to provider-neutral errors."""
    if isinstance(exc, ProviderError):
        return exc
    yf_exc = _yf().exceptions
    text = f"{type(exc).__name__}: {exc}"
    lowered = text.lower()
    kw = dict(provider=PROVIDER_CODE, symbol=symbol)
    if isinstance(exc, yf_exc.YFRateLimitError) or "too many requests" in lowered or "429" in lowered:
        return RateLimited(text, **kw)
    if isinstance(exc, yf_exc.YFInvalidPeriodError) or "must be within the last" in lowered:
        return InvalidRequest(text, **kw)
    if isinstance(exc, (yf_exc.YFPricesMissingError, yf_exc.YFTickerMissingError, yf_exc.YFTzMissingError)):
        return NoDataReturned(text, **kw)
    return ProviderUnavailable(text, **kw)


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class YahooFinanceProvider:
    """Implements HistoricalDataProvider, IntradayDataProvider, QuoteDataProvider and
    InstrumentMetadataProvider. Whether each capability *works* is established by the opt-in probe,
    not assumed here (see DESCRIPTOR)."""

    descriptor = DESCRIPTOR

    def __init__(self, timeout_seconds: int = 15, ticker_factory: Callable[[str], Any] | None = None):
        self._timeout = timeout_seconds
        self._ticker_factory = ticker_factory or (lambda symbol: _yf().Ticker(symbol))

    # ---- candles ---------------------------------------------------------------------------

    def fetch_history(self, provider_symbol: str, interval: str, start: datetime, end: datetime) -> ProviderBarsResult:
        if interval not in SUPPORTED_INTERVALS:
            raise InvalidRequest(f"unsupported interval {interval!r}", provider=PROVIDER_CODE, symbol=provider_symbol)
        if start.tzinfo is None or end.tzinfo is None:
            raise InvalidRequest("start and end must be timezone-aware", provider=PROVIDER_CODE, symbol=provider_symbol)
        if end <= start:
            raise InvalidRequest("end must be after start", provider=PROVIDER_CODE, symbol=provider_symbol)
        try:
            ticker = self._ticker_factory(provider_symbol)
            frame = ticker.history(
                interval=SUPPORTED_INTERVALS[interval], start=start, end=end, timeout=self._timeout, **HISTORY_PARAMS
            )
        except Exception as exc:  # noqa: BLE001 - translated, never swallowed
            raise _translate(exc, provider_symbol) from exc
        observed_at = datetime.now(UTC)
        bars = self._frame_to_bars(frame, provider_symbol)
        return ProviderBarsResult(
            provider_code=PROVIDER_CODE,
            provider_symbol=provider_symbol,
            interval=interval,
            request_start=start,
            request_end=end,
            bars=bars,
            observed_at=observed_at,
            reported_timezone=self._reported_timezone(ticker),
            meta={
                "library": "yfinance",
                "library_version": _yf().__version__,
                "params": {k: v for k, v in HISTORY_PARAMS.items()},
                "columns": [str(c) for c in frame.columns],
                "index_tz": str(getattr(frame.index, "tz", None)),
                "rows": int(len(frame)),
            },
        )

    def fetch_latest_intraday(self, provider_symbol: str, interval: str, lookback: timedelta) -> ProviderBarsResult:
        now = datetime.now(UTC)
        return self.fetch_history(provider_symbol, interval, now - lookback, now)

    def _frame_to_bars(self, frame: Any, symbol: str) -> list[RawBar]:
        if frame is None or len(frame) == 0:
            raise NoDataReturned("empty response", provider=PROVIDER_CODE, symbol=symbol)
        missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
        if missing:
            raise MalformedResponse(f"missing columns {missing}", provider=PROVIDER_CODE, symbol=symbol)
        has_adj = "Adj Close" in frame.columns
        bars: list[RawBar] = []
        for ts, row in frame.iterrows():
            try:
                start_ts = ts.to_pydatetime()
            except AttributeError:
                start_ts = None  # not a timestamp: validation will reject it
            extras: dict[str, Any] = {}
            if has_adj:
                extras["adj_close"] = _to_float(row.get("Adj Close"))
            for column, key in (("Dividends", "dividends"), ("Stock Splits", "stock_splits")):
                value = _to_float(row.get(column)) if column in frame.columns else None
                if value:
                    extras[key] = value
            bars.append(
                RawBar(
                    start_ts=start_ts,
                    open=_to_float(row["Open"]),
                    high=_to_float(row["High"]),
                    low=_to_float(row["Low"]),
                    close=_to_float(row["Close"]),
                    volume=_to_float(row["Volume"]),
                    extras=extras,
                )
            )
        return bars

    @staticmethod
    def _reported_timezone(ticker: Any) -> str | None:
        try:
            meta = ticker.get_history_metadata()
            tz = meta.get("exchangeTimezoneName") if isinstance(meta, dict) else None
            return str(tz) if tz else None
        except Exception:  # noqa: BLE001 - informational only
            return None

    # ---- corporate actions -----------------------------------------------------------------

    def fetch_corporate_actions(self, provider_symbol: str) -> list[RawCorporateAction]:
        try:
            frame = self._ticker_factory(provider_symbol).get_actions()
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc, provider_symbol) from exc
        if frame is None or len(frame) == 0:
            return []
        actions: list[RawCorporateAction] = []
        for ts, row in frame.iterrows():
            ex_date = ts.date() if hasattr(ts, "date") else None
            if ex_date is None:
                continue
            div = _to_float(row.get("Dividends")) if "Dividends" in frame.columns else None
            split = _to_float(row.get("Stock Splits")) if "Stock Splits" in frame.columns else None
            if div and math.isfinite(div) and div > 0:
                actions.append(RawCorporateAction(ex_date, "DIVIDEND", div))
            if split and math.isfinite(split) and split > 0:
                actions.append(RawCorporateAction(ex_date, "SPLIT", split))
        return actions

    # ---- quote and metadata ----------------------------------------------------------------

    def fetch_quote(self, provider_symbol: str) -> ProviderQuote:
        try:
            info = self._ticker_factory(provider_symbol).fast_info
            price = _to_float(info["lastPrice"])
            currency = info["currency"]
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc, provider_symbol) from exc
        if price is None or not math.isfinite(price):
            raise NoDataReturned("no last price", provider=PROVIDER_CODE, symbol=provider_symbol)
        return ProviderQuote(
            provider_symbol=provider_symbol,
            last_price=Decimal(str(price)),
            currency=str(currency) if currency else None,
            observed_at=datetime.now(UTC),
            market_ts=None,  # Yahoo's fast_info does not say when this price occurred
            meta={"note": "market timestamp unknown: Yahoo does not say when this price occurred"},
        )

    def fetch_metadata(self, provider_symbol: str) -> ProviderInstrumentMetadata:
        try:
            info = self._ticker_factory(provider_symbol).fast_info
            exchange, currency, tz = info["exchange"], info["currency"], info["timezone"]
        except Exception as exc:  # noqa: BLE001
            raise _translate(exc, provider_symbol) from exc
        return ProviderInstrumentMetadata(
            provider_symbol=provider_symbol,
            exchange=str(exchange) if exchange else None,
            currency=str(currency) if currency else None,
            timezone=str(tz) if tz else None,
            name=None,
            meta={},
        )

    def fetch_unsupported(self, capability: Capability):  # pragma: no cover - guard for explicit misuse
        raise UnsupportedCapability(f"{capability.value} not supported", provider=PROVIDER_CODE)
