"""The Yahoo adapter against a scripted fake ticker: no network, no real yfinance calls."""

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

import yfinance.exceptions as yfe

from app.marketdata.errors import (
    InvalidRequest,
    MalformedResponse,
    NoDataReturned,
    ProviderUnavailable,
    RateLimited,
)
from app.marketdata.providers.base import (
    HistoricalDataProvider,
    InstrumentMetadataProvider,
    IntradayDataProvider,
    QuoteDataProvider,
)
from app.marketdata.providers.yahoo import DESCRIPTOR, HISTORY_PARAMS, YahooFinanceProvider
from app.marketdata.types import Capability, CapabilityStatus

START = datetime(2026, 11, 10, 3, 0, tzinfo=UTC)
END = datetime(2026, 11, 11, 3, 0, tzinfo=UTC)


def frame(rows=None, tz="Asia/Kolkata", columns=("Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits")):
    rows = rows if rows is not None else [
        ("2026-11-11 09:15", 100.0, 101.0, 99.0, 100.5, 100.5, 5000, 0.0, 0.0),
        ("2026-11-11 09:20", 100.5, 102.0, 100.0, 101.5, 101.5, 6000, 0.0, 0.0),
    ]
    idx = pd.DatetimeIndex([r[0] for r in rows]) if rows else pd.DatetimeIndex([])
    if tz and len(idx):
        idx = idx.tz_localize(tz)
    df = pd.DataFrame([r[1:] for r in rows], index=idx, columns=list(columns)) if rows else pd.DataFrame(columns=list(columns))
    return df


class FakeTicker:
    def __init__(self, history=None, raises=None, actions=None, fast=None):
        self._history, self._raises, self._actions, self._fast = history, raises, actions, fast
        self.history_kwargs = None

    def history(self, **kwargs):
        self.history_kwargs = kwargs
        if self._raises:
            raise self._raises
        return self._history

    def get_history_metadata(self):
        return {"exchangeTimezoneName": "Asia/Kolkata"}

    def get_actions(self):
        if self._raises:
            raise self._raises
        return self._actions

    @property
    def fast_info(self):
        if self._raises:
            raise self._raises
        return self._fast


def provider(ticker):
    seen = []

    def factory(symbol):
        seen.append(symbol)
        return ticker

    p = YahooFinanceProvider(ticker_factory=factory)
    p.seen = seen
    return p


# ---- request parameters ----------------------------------------------------------------------


def test_every_adjustment_related_parameter_is_set_explicitly():
    t = FakeTicker(frame())
    provider(t).fetch_history("RELIANCE.NS", "5m", START, END)
    kw = t.history_kwargs
    assert kw["auto_adjust"] is False and kw["back_adjust"] is False and kw["repair"] is False
    assert kw["actions"] is True and kw["prepost"] is False and kw["rounding"] is False
    assert kw["raise_errors"] is True  # failures must raise, never come back as a silent empty frame
    assert kw["keepna"] is True  # NaN rows must reach the validation layer instead of vanishing
    assert kw["interval"] == "5m" and kw["start"] == START and kw["end"] == END
    assert HISTORY_PARAMS["auto_adjust"] is False


def test_the_provider_symbol_is_passed_through_unchanged():
    p = provider(FakeTicker(frame()))
    p.fetch_history("HDFCBANK.NS", "5m", START, END)
    assert p.seen == ["HDFCBANK.NS"]


def test_installed_yfinance_version_is_pinned_and_recorded():
    import yfinance

    assert yfinance.__version__ == "1.8.0"
    res = provider(FakeTicker(frame())).fetch_history("X.NS", "5m", START, END)
    assert res.meta["library_version"] == "1.8.0" and res.meta["params"]["auto_adjust"] is False


# ---- response parsing ------------------------------------------------------------------------


def test_frame_becomes_provider_neutral_raw_bars():
    res = provider(FakeTicker(frame())).fetch_history("X.NS", "5m", START, END)
    assert [type(b).__name__ for b in res.bars] == ["RawBar", "RawBar"]
    b = res.bars[0]
    assert (b.open, b.high, b.low, b.close, b.volume) == (100.0, 101.0, 99.0, 100.5, 5000.0)
    assert b.start_ts.tzinfo is not None and b.start_ts.utcoffset() == timedelta(hours=5, minutes=30)
    assert b.extras["adj_close"] == 100.5
    assert res.reported_timezone == "Asia/Kolkata" and res.provider_code == "yahoo" and res.observed_at.tzinfo is not None
    assert res.meta["index_tz"] == "Asia/Kolkata"


def test_naive_index_is_passed_through_as_naive_so_validation_can_reject_it():
    res = provider(FakeTicker(frame(tz=None))).fetch_history("X.NS", "5m", START, END)
    assert res.bars[0].start_ts.tzinfo is None  # the adapter never guesses a timezone


def test_nan_rows_are_preserved_for_the_validation_layer():
    rows = [("2026-11-11 15:20", float("nan"), float("nan"), float("nan"), float("nan"), float("nan"), 0, 0.0, 0.0)]
    res = provider(FakeTicker(frame(rows))).fetch_history("X.NS", "5m", START, END)
    assert len(res.bars) == 1 and res.bars[0].close != res.bars[0].close  # NaN kept, not dropped or filled


def test_dividends_and_splits_are_carried_as_extras_only_when_present():
    rows = [("2026-11-11 09:15", 1, 2, 1, 1.5, 1.5, 10, 2.5, 0.0), ("2026-11-11 09:20", 1, 2, 1, 1.5, 1.5, 10, 0.0, 2.0)]
    bars = provider(FakeTicker(frame(rows))).fetch_history("X.NS", "5m", START, END).bars
    assert bars[0].extras.get("dividends") == 2.5 and "stock_splits" not in bars[0].extras
    assert bars[1].extras.get("stock_splits") == 2.0 and "dividends" not in bars[1].extras


def test_missing_adj_close_column_is_tolerated():
    cols = ("Open", "High", "Low", "Close", "Volume")
    rows = [("2026-11-11 09:15", 1, 2, 1, 1.5, 10)]
    res = provider(FakeTicker(frame(rows, columns=cols))).fetch_history("X.NS", "5m", START, END)
    assert "adj_close" not in res.bars[0].extras


# ---- failure handling ------------------------------------------------------------------------


def test_empty_frame_raises_no_data():
    with pytest.raises(NoDataReturned):
        provider(FakeTicker(frame([]))).fetch_history("X.NS", "5m", START, END)


def test_none_frame_raises_no_data():
    with pytest.raises(NoDataReturned):
        provider(FakeTicker(None)).fetch_history("X.NS", "5m", START, END)


def test_missing_required_columns_is_malformed():
    bad = frame(columns=("Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits")).drop(columns=["Close"])
    with pytest.raises(MalformedResponse):
        provider(FakeTicker(bad)).fetch_history("X.NS", "5m", START, END)


@pytest.mark.parametrize(
    "exc,expected",
    [
        (yfe.YFRateLimitError(), RateLimited),
        (Exception("HTTP Error 429: Too Many Requests"), RateLimited),
        (yfe.YFPricesMissingError("X.NS", "5m"), NoDataReturned),
        (yfe.YFTickerMissingError("X.NS", "delisted"), NoDataReturned),
        (yfe.YFInvalidPeriodError("X.NS", "99y", "1d,5d"), InvalidRequest),
        (Exception("5m data not available. The requested range must be within the last 60 days."), InvalidRequest),
        (ConnectionError("network down"), ProviderUnavailable),
        (RuntimeError("something unexpected"), ProviderUnavailable),
    ],
)
def test_library_failures_are_translated_to_provider_neutral_errors(exc, expected):
    with pytest.raises(expected) as info:
        provider(FakeTicker(raises=exc)).fetch_history("X.NS", "5m", START, END)
    assert info.value.provider == "yahoo" and info.value.symbol == "X.NS"
    assert info.value.kind  # a stable label for persistence


def test_the_original_exception_is_chained_not_swallowed():
    with pytest.raises(ProviderUnavailable) as info:
        provider(FakeTicker(raises=ConnectionError("boom"))).fetch_history("X.NS", "5m", START, END)
    assert isinstance(info.value.__cause__, ConnectionError)


@pytest.mark.parametrize("interval", ["2m", "90m", "1wk", "", "bogus"])
def test_unsupported_intervals_are_refused_before_any_request(interval):
    t = FakeTicker(frame())
    with pytest.raises(InvalidRequest):
        provider(t).fetch_history("X.NS", interval, START, END)
    assert t.history_kwargs is None


def test_naive_request_bounds_and_inverted_ranges_are_refused():
    t = FakeTicker(frame())
    p = provider(t)
    with pytest.raises(InvalidRequest):
        p.fetch_history("X.NS", "5m", datetime(2026, 11, 10), END)
    with pytest.raises(InvalidRequest):
        p.fetch_history("X.NS", "5m", END, START)
    assert t.history_kwargs is None


# ---- other capabilities -----------------------------------------------------------------------


def test_corporate_actions_are_parsed_from_daily_rows():
    idx = pd.DatetimeIndex(["2024-10-28", "2025-06-05", "2025-07-01"]).tz_localize("Asia/Kolkata")
    actions = pd.DataFrame({"Dividends": [0.0, 5.5, 0.0], "Stock Splits": [2.0, 0.0, 0.0]}, index=idx)
    out = provider(FakeTicker(actions=actions)).fetch_corporate_actions("X.NS")
    assert [(a.ex_date, a.kind, a.value) for a in out] == [(date(2024, 10, 28), "SPLIT", 2.0), (date(2025, 6, 5), "DIVIDEND", 5.5)]


def test_no_corporate_actions_is_an_empty_list():
    assert provider(FakeTicker(actions=pd.DataFrame())).fetch_corporate_actions("X.NS") == []
    assert provider(FakeTicker(actions=None)).fetch_corporate_actions("X.NS") == []


def test_corporate_action_failure_is_translated():
    with pytest.raises(RateLimited):
        provider(FakeTicker(raises=yfe.YFRateLimitError())).fetch_corporate_actions("X.NS")


def test_quote_has_no_market_timestamp_because_yahoo_gives_none():
    q = provider(FakeTicker(fast={"lastPrice": 1338.5999755859375, "currency": "INR"})).fetch_quote("X.NS")
    assert q.currency == "INR" and q.market_ts is None and q.observed_at.tzinfo is not None
    assert float(q.last_price) == 1338.5999755859375


def test_quote_without_a_price_is_no_data():
    with pytest.raises(NoDataReturned):
        provider(FakeTicker(fast={"lastPrice": float("nan"), "currency": "INR"})).fetch_quote("X.NS")


def test_metadata_reports_what_the_provider_says_without_inventing_a_name():
    m = provider(FakeTicker(fast={"exchange": "NSI", "currency": "INR", "timezone": "Asia/Kolkata"})).fetch_metadata("X.NS")
    assert (m.exchange, m.currency, m.timezone, m.name) == ("NSI", "INR", "Asia/Kolkata", None)


def test_latest_intraday_requests_a_window_ending_now():
    t = FakeTicker(frame())
    provider(t).fetch_latest_intraday("X.NS", "5m", timedelta(hours=2))
    assert t.history_kwargs["end"] - t.history_kwargs["start"] == timedelta(hours=2)
    assert abs((datetime.now(UTC) - t.history_kwargs["end"]).total_seconds()) < 5


# ---- interfaces and honesty of declarations ------------------------------------------------------


def test_adapter_satisfies_all_four_provider_interfaces():
    p = YahooFinanceProvider()
    for proto in (HistoricalDataProvider, IntradayDataProvider, QuoteDataProvider, InstrumentMetadataProvider):
        assert isinstance(p, proto)


def test_static_declarations_never_claim_a_verified_capability():
    """VERIFIED only ever comes from a stored probe result, never from code."""
    assert all(d.status in (CapabilityStatus.DOCUMENTED, CapabilityStatus.UNKNOWN) for d in DESCRIPTOR.declared)
    by_cap = {d.capability: d.status for d in DESCRIPTOR.declared}
    assert by_cap[Capability.FUTURES_CONTRACT_DATA] is CapabilityStatus.UNKNOWN
    assert by_cap[Capability.QUOTE_RETRIEVAL] is CapabilityStatus.UNKNOWN


def test_only_the_measured_interval_has_a_lookback_limit():
    assert DESCRIPTOR.max_lookback_by_interval == {"5m": timedelta(days=60)}
    assert DESCRIPTOR.lookback_safety > timedelta(0)
