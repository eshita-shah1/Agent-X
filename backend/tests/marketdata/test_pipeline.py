import math
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.marketdata.calendar import EXCHANGE_TZ, TradingCalendar
from app.marketdata.pipeline import process_bars, to_decimal
from app.marketdata.types import IssueType, ProviderBarsResult, RawBar, Severity

CAL = TradingCalendar()
MARGIN = timedelta(minutes=5)
OBS = datetime(2026, 11, 11, 11, 0, tzinfo=UTC)  # 16:30 IST, after the close
NAN = float("nan")


def ist(h, m, day=11, month=11):
    return datetime(2026, month, day, h, m, tzinfo=EXCHANGE_TZ)


def bar(ts=None, o=100.0, h=101.0, l=99.0, c=100.5, v=1000.0, **extras):
    return RawBar(ts if ts is not None else ist(10, 0), o, h, l, c, v, extras)


def run(bars, observed_at=OBS, margin=MARGIN):
    res = ProviderBarsResult("yahoo", "X.NS", "5m", OBS - timedelta(days=1), OBS, bars, observed_at)
    return process_bars(res, instrument_id=1, timeframe="5m", calendar=CAL, finalization_margin=margin)


def types(p):
    return [i.issue_type for i in p.issues]


def test_valid_bar_becomes_a_utc_candle_with_exchange_session_date():
    p = run([bar(ist(10, 0))])
    assert len(p.candles) == 1 and not p.issues
    c = p.candles[0]
    assert c.bar_start_ts == datetime(2026, 11, 11, 4, 30, tzinfo=UTC)
    assert c.bar_end_ts - c.bar_start_ts == timedelta(minutes=5)
    assert c.bar_start_ts.tzinfo is UTC
    assert c.session_date.isoformat() == "2026-11-11"
    assert c.provider_code == "yahoo" and c.provider_symbol == "X.NS" and c.observed_at == OBS


def test_utc_aware_timestamp_is_equivalent_to_ist_aware():
    a = run([bar(ist(10, 0))]).candles[0]
    b = run([bar(datetime(2026, 11, 11, 4, 30, tzinfo=UTC))]).candles[0]
    assert a.bar_start_ts == b.bar_start_ts


def test_other_fixed_offset_is_converted_correctly():
    plus9 = timezone(timedelta(hours=9))
    assert run([bar(datetime(2026, 11, 11, 13, 30, tzinfo=plus9))]).candles[0].bar_start_ts == datetime(2026, 11, 11, 4, 30, tzinfo=UTC)


def test_naive_timestamp_is_rejected_not_assumed_utc():
    p = run([bar(datetime(2026, 11, 11, 10, 0))])
    assert not p.candles and types(p) == [IssueType.NAIVE_TIMESTAMP] and p.quarantined == 1


def test_missing_timestamp_is_rejected():
    p = run([RawBar(None, 1, 2, 1, 1.5, 10)])
    assert not p.candles and types(p) == [IssueType.INVALID_TIMESTAMP]


def test_misaligned_timestamp_is_rejected():
    p = run([bar(ist(10, 2))])
    assert not p.candles and types(p) == [IssueType.MISALIGNED_TIMESTAMP]


def test_timestamp_with_seconds_is_misaligned():
    p = run([bar(datetime(2026, 11, 11, 10, 0, 30, tzinfo=EXCHANGE_TZ))])
    assert types(p) == [IssueType.MISALIGNED_TIMESTAMP]


@pytest.mark.parametrize("ts", [ist(9, 10), ist(15, 30), ist(18, 0), ist(8, 0), ist(10, 0, day=14)])
def test_bars_outside_the_session_are_quarantined_not_dropped_silently(ts):
    p = run([bar(ts)])
    assert not p.candles and types(p) == [IssueType.OUT_OF_SESSION] and p.quarantined == 1
    assert p.issues[0].details["raw"]["close"] == 100.5  # the rejected row is preserved for inspection


def test_all_nan_row_is_a_provider_gap_marker_and_quarantined():
    p = run([bar(ist(15, 20), NAN, NAN, NAN, NAN, 0.0)])
    assert not p.candles
    issue = p.issues[0]
    assert issue.issue_type is IssueType.NON_NUMERIC_VALUE and issue.severity is Severity.WARNING
    assert issue.details["provider_gap_marker"] is True


def test_quarantined_payloads_are_json_safe_because_postgres_rejects_nan():
    """Regression: a gap-marker row with NaN prices once crashed the whole batch at the JSONB insert."""
    import json

    p = run([bar(ist(15, 20), NAN, NAN, NAN, NAN, 0.0)])
    payload = p.issues[0].details["raw"]
    assert payload["open"] == "nan" and payload["volume"] == 0.0
    json.dumps(p.issues[0].details, allow_nan=False)  # strict JSON: would raise on a bare NaN


def test_json_safe_handles_nested_and_infinite_values():
    from app.marketdata.pipeline import json_safe

    out = json_safe({"a": [float("inf"), {"b": float("-inf"), "c": 1.5}], "d": float("nan")})
    assert out == {"a": ["inf", {"b": "-inf", "c": 1.5}], "d": "nan"}


def test_partially_nan_row_is_an_error():
    p = run([bar(o=NAN)])
    assert types(p) == [IssueType.NON_NUMERIC_VALUE] and p.issues[0].severity is Severity.ERROR
    assert "provider_gap_marker" not in p.issues[0].details


def test_none_price_is_treated_as_missing():
    p = run([RawBar(ist(10, 0), None, None, None, None, 5.0)])
    assert types(p) == [IssueType.NON_NUMERIC_VALUE]


@pytest.mark.parametrize("field", ["o", "h", "l", "c"])
def test_infinite_price_is_rejected(field):
    p = run([bar(**{field: math.inf})])
    assert types(p) == [IssueType.NON_FINITE_VALUE]


@pytest.mark.parametrize("o,h,l,c", [(0, 1, 0, 0.5), (-1, 2, -1, 1), (100, 101, 99, 0)])
def test_non_positive_price_is_rejected(o, h, l, c):
    assert types(run([bar(o=o, h=h, l=l, c=c)])) == [IssueType.NON_POSITIVE_PRICE]


@pytest.mark.parametrize(
    "o,h,l,c",
    [(100, 99, 98, 99), (100, 101, 102, 100.5), (105, 101, 99, 100), (100, 101, 99, 98), (100, 101, 99.5, 99)],
)
def test_ohlc_relationship_violations_are_rejected(o, h, l, c):
    assert types(run([bar(o=o, h=h, l=l, c=c)])) == [IssueType.INVALID_OHLC]


def test_flat_bar_with_equal_ohlc_is_valid():
    assert len(run([bar(o=100, h=100, l=100, c=100)]).candles) == 1


@pytest.mark.parametrize("v", [-1.0, NAN, math.inf, 10.5, None])
def test_invalid_volume_is_rejected(v):
    assert types(run([bar(v=v)])) == [IssueType.INVALID_VOLUME]


def test_zero_volume_with_real_prices_is_valid():
    p = run([bar(v=0.0)])
    assert len(p.candles) == 1 and p.candles[0].volume == 0


def test_whole_number_float_volume_becomes_int():
    assert run([bar(v=34158.0)]).candles[0].volume == 34158


def test_identical_duplicates_collapse_to_one_row_with_an_info_issue():
    p = run([bar(ist(10, 0)), bar(ist(10, 0))])
    assert len(p.candles) == 1 and p.duplicates_collapsed == 1
    assert types(p) == [IssueType.DUPLICATE_IDENTICAL] and p.issues[0].severity is Severity.INFO


def test_conflicting_duplicates_are_all_quarantined():
    p = run([bar(ist(10, 0), c=100.5), bar(ist(10, 0), c=100.6)])
    assert not p.candles and p.quarantined == 2
    assert types(p) == [IssueType.DUPLICATE_CONFLICTING] * 2


def test_duplicates_via_different_offsets_are_still_detected():
    p = run([bar(ist(10, 0)), bar(datetime(2026, 11, 11, 4, 30, tzinfo=UTC))])
    assert len(p.candles) == 1 and p.duplicates_collapsed == 1


def test_a_bar_is_final_only_after_its_end_plus_the_margin():
    end = ist(10, 5).astimezone(UTC)
    not_yet = run([bar(ist(10, 0))], observed_at=end + timedelta(minutes=4, seconds=59))
    assert not not_yet.candles and not_yet.non_final_dropped == 1 and types(not_yet) == [IssueType.INCOMPLETE_CANDLE]
    exactly = run([bar(ist(10, 0))], observed_at=end + MARGIN)
    assert len(exactly.candles) == 1


def test_in_progress_bar_is_never_archived():
    # observed at 10:02 IST: the 10:00 bar is still forming (end 10:05)
    p = run([bar(ist(9, 55)), bar(ist(10, 0))], observed_at=ist(10, 2).astimezone(UTC))
    assert [c.bar_start_ts for c in p.candles] == [] and p.non_final_dropped == 2  # margin keeps even 09:55 out


def test_zero_margin_still_excludes_an_unfinished_bar():
    p = run([bar(ist(10, 0))], observed_at=ist(10, 3).astimezone(UTC), margin=timedelta(0))
    assert not p.candles and p.non_final_dropped == 1


def test_mixed_batch_keeps_good_rows_and_reports_each_bad_one():
    rows = [bar(ist(10, 0)), bar(ist(10, 5), h=50.0), bar(ist(10, 10)), bar(ist(15, 20), NAN, NAN, NAN, NAN, 0.0)]
    p = run(rows)
    assert len(p.candles) == 2 and p.quarantined == 2 and p.rows_received == 4
    assert sorted(types(p), key=str) == sorted([IssueType.INVALID_OHLC, IssueType.NON_NUMERIC_VALUE], key=str)


def test_candles_are_returned_in_time_order_regardless_of_input_order():
    p = run([bar(ist(10, 10)), bar(ist(10, 0)), bar(ist(10, 5))])
    assert [c.bar_start_ts.astimezone(EXCHANGE_TZ).strftime("%H:%M") for c in p.candles] == ["10:00", "10:05", "10:10"]


def test_response_bounds_cover_parseable_rows_only():
    p = run([bar(ist(10, 0)), bar(ist(10, 30)), bar(datetime(2026, 11, 11, 10, 0))])
    assert p.first_ts == ist(10, 0).astimezone(UTC) and p.last_ts == ist(10, 30).astimezone(UTC)


def test_float32_noise_is_quantised_to_six_places_deterministically():
    # real Yahoo value: a float32 artefact of 1338.60
    assert to_decimal(1338.5999755859375) == Decimal("1338.599976")
    assert to_decimal(100.0) == Decimal("100.000000")
    assert to_decimal(2905.7) == to_decimal(2905.7)


def test_quantisation_uses_bankers_rounding():
    assert to_decimal(0.0000005) == Decimal("0.000000")  # half-even at the 7th place
    assert to_decimal(0.0000015) == Decimal("0.000002")


def test_prices_are_decimals_and_source_metadata_records_the_library():
    res = ProviderBarsResult("yahoo", "X.NS", "5m", OBS, OBS, [bar()], OBS, meta={"library": "yfinance", "library_version": "1.8.0", "params": {"auto_adjust": False}})
    c = process_bars(res, instrument_id=1, timeframe="5m", calendar=CAL, finalization_margin=MARGIN).candles[0]
    assert all(isinstance(x, Decimal) for x in (c.open, c.high, c.low, c.close))
    assert c.source_metadata["library_version"] == "1.8.0" and c.source_metadata["request_params"] == {"auto_adjust": False}


def test_extremely_large_price_is_rejected():
    assert types(run([bar(o=1e12, h=1e12, l=1e12, c=1e12)])) == [IssueType.NON_POSITIVE_PRICE]


# ---- measured provider artefacts: truncated first bar, bars cut off by the request end -----------------------


def run_window(bars, request_start, request_end=OBS, observed_at=OBS, **kw):
    res = ProviderBarsResult("yahoo", "X.NS", "5m", request_start, request_end, bars, observed_at)
    return process_bars(res, instrument_id=1, timeframe="5m", calendar=CAL, finalization_margin=MARGIN, **kw)


def test_the_truncated_leading_edge_bar_is_quarantined_not_archived():
    rows = [bar(ist(10, 0), v=76930), bar(ist(10, 5)), bar(ist(10, 10))]
    p = run_window(rows, ist(10, 2).astimezone(UTC), discard_leading_edge_bar=True)
    assert [c.bar_start_ts.astimezone(EXCHANGE_TZ).strftime("%H:%M") for c in p.candles] == ["10:05", "10:10"]
    assert types(p) == [IssueType.WINDOW_EDGE_TRUNCATED] and p.quarantined == 1
    assert p.issues[0].details["raw"]["volume"] == 76930  # kept for inspection


def test_even_a_bar_aligned_start_truncates_the_first_bar():
    """Measured: a start exactly on a bar boundary inside a session still truncated the first bar."""
    rows = [bar(ist(11, 5)), bar(ist(11, 10))]
    p = run_window(rows, ist(11, 5).astimezone(UTC), discard_leading_edge_bar=True)
    assert len(p.candles) == 1 and p.candles[0].bar_start_ts.astimezone(EXCHANGE_TZ).strftime("%H:%M") == "11:10"


def test_without_the_flag_the_first_bar_is_treated_like_any_other():
    p = run_window([bar(ist(10, 0)), bar(ist(10, 5))], ist(10, 2).astimezone(UTC))
    assert len(p.candles) == 2 and not p.issues


def test_a_first_bar_far_from_the_request_start_is_not_mistaken_for_the_edge_bar():
    # request started overnight; the first bar is the next session's open: nothing was truncated
    p = run_window([bar(ist(9, 15)), bar(ist(9, 20))], ist(18, 0, day=10).astimezone(UTC), discard_leading_edge_bar=True)
    assert len(p.candles) == 2 and not p.issues


def test_only_the_single_leading_bar_is_ever_discarded():
    rows = [bar(ist(10, 0)), bar(ist(10, 5)), bar(ist(10, 10)), bar(ist(10, 15))]
    p = run_window(rows, ist(10, 0).astimezone(UTC), discard_leading_edge_bar=True)
    assert len(p.candles) == 3 and p.quarantined == 1


def test_a_bar_that_ends_after_the_request_end_is_incomplete_whatever_the_clock_says():
    rows = [bar(ist(10, 0)), bar(ist(10, 5))]
    p = run_window(rows, ist(9, 0).astimezone(UTC), request_end=ist(10, 7).astimezone(UTC), observed_at=ist(16, 0).astimezone(UTC))
    assert [c.bar_start_ts.astimezone(EXCHANGE_TZ).strftime("%H:%M") for c in p.candles] == ["10:00"]
    assert p.issues[0].details["ends_after_request_end"] is True


def test_empty_response_yields_nothing():
    p = run([])
    assert not p.candles and not p.issues and p.rows_received == 0
