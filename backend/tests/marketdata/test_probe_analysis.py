"""The probe's pure analysis functions, against fixtures shaped like what Yahoo actually returned."""

import json
from datetime import UTC, date, datetime, timedelta

from app.marketdata.calendar import EXCHANGE_TZ
from app.marketdata.probe import (
    analyze_daily_vs_intraday,
    analyze_intraday,
    analyze_price_basis,
    compare_overlap,
    derive_capabilities,
    derive_price_basis_observations,
    jsonable,
    summarize,
)
from app.marketdata.types import RawBar
from tests.support import CAL, NOW, day_bars, days_bars

NAN = float("nan")
OBS = datetime(2026, 11, 11, 11, 0, tzinfo=UTC)  # after the close of 11 Nov


def analyze(bars, observed_at=OBS):
    return analyze_intraday(bars, interval="5m", observed_at=observed_at, request_start=OBS - timedelta(days=5), request_end=OBS)


def test_clean_days_report_no_gaps():
    a = analyze(days_bars([date(2026, 11, 9), date(2026, 11, 10)]))
    assert a["returned_candles"] == 150 and a["days_complete"] == 2
    assert a["missing_interval_total_excl_absent_days"] == 0 and a["absent_weekday_sessions"] == []
    assert a["bar_start_time_of_day_min_max_ist"] == ["09:15", "15:25"]
    assert a["duplicate_timestamps"] == 0 and a["out_of_session_bar_count"] == 0 and a["misaligned_timestamps"] == 0


def test_nan_rows_count_as_missing_not_present():
    bars = day_bars(date(2026, 11, 10))
    bars[-2] = RawBar(bars[-2].start_ts, NAN, NAN, NAN, NAN, 0.0)  # the 15:20 bar, as Yahoo returns it
    a = analyze(bars)
    assert a["nan_price_rows"] == 1 and a["missing_interval_total_excl_absent_days"] == 1
    assert a["invalid_rows_by_time_of_day_ist"] == {"15:20": 1}
    assert "2026-11-10" in a["partial_days"]


def test_an_absent_weekday_between_two_present_days_is_flagged_as_absent():
    a = analyze(days_bars([date(2026, 11, 9), date(2026, 11, 11)]))
    assert a["absent_weekday_sessions"] == [date(2026, 11, 10)]


def test_future_slots_of_today_are_not_missing():
    now = datetime(2026, 11, 11, 7, 40, tzinfo=UTC)  # 13:10 IST
    a = analyze_intraday(day_bars(date(2026, 11, 11), last=47), interval="5m", observed_at=now, request_start=now - timedelta(days=1), request_end=now)
    assert a["missing_interval_total_excl_absent_days"] == 0


def test_in_progress_last_bar_shows_as_negative_lag():
    now = datetime(2026, 11, 11, 7, 37, tzinfo=UTC)  # 13:07 IST, bar 13:05-13:10 still forming
    a = analyze_intraday(day_bars(date(2026, 11, 11), last=48), interval="5m", observed_at=now, request_start=now - timedelta(days=1), request_end=now)
    assert a["last_bar_lag_minutes"] < 0


def test_duplicates_naive_and_out_of_session_rows_are_counted():
    bars = day_bars(date(2026, 11, 10), last=3)
    bars += [bars[0]]  # duplicate
    bars += [RawBar(datetime(2026, 11, 10, 10, 0), 1, 2, 1, 1.5, 5)]  # naive
    bars += [RawBar(datetime(2026, 11, 10, 19, 0, tzinfo=EXCHANGE_TZ), 1, 2, 1, 1.5, 5)]  # out of session
    a = analyze(bars)
    assert a["duplicate_timestamps"] == 1 and a["naive_or_missing_timestamps"] == 1 and a["out_of_session_bar_count"] == 1


def test_float_noise_is_measured():
    bars = [RawBar(b.start_ts, 1338.5999755859375, 1338.8000488, 1337.5999755859375, 1338.1999512, 10) for b in day_bars(date(2026, 11, 10), last=2)]
    a = analyze(bars)
    assert a["price_values_not_on_2dp"] == 8 and 0 < a["max_deviation_from_2dp"] < 1e-3


def test_intraday_adj_close_differences_are_detected():
    bars = day_bars(date(2026, 11, 10), last=3)
    same = [RawBar(b.start_ts, b.open, b.high, b.low, b.close, b.volume, {"adj_close": b.close}) for b in bars]
    diff = [RawBar(b.start_ts, b.open, b.high, b.low, b.close, b.volume, {"adj_close": b.close * 0.9}) for b in bars]
    assert analyze(same)["intraday_adj_close_differs_from_close"] == 0
    assert analyze(diff)["intraday_adj_close_differs_from_close"] == 3


def test_no_timezone_aware_rows_is_reported_not_crashed():
    a = analyze([RawBar(datetime(2026, 11, 10, 10, 0), 1, 2, 1, 1.5, 5)])
    assert a["note"] == "no timezone-aware timestamps returned"


# ---- overlap / restatement observation ----------------------------------------------------------------


def test_identical_refetch_shows_no_changes():
    b = day_bars(date(2026, 11, 10))
    r = compare_overlap(b, list(b))
    assert r["compared_bars"] == 75 and r["changed_bars"] == 0 and r["last_bar_changed"] is False


def test_changed_last_bar_is_flagged_as_a_partial_bar():
    a = day_bars(date(2026, 11, 10), last=10)
    b = list(a)
    b[-1] = RawBar(b[-1].start_ts, b[-1].open, b[-1].high, b[-1].low, b[-1].close + 0.2, b[-1].volume)
    r = compare_overlap(a, b)
    assert r["changed_bars"] == 1 and r["last_bar_changed"] is True and r["changed_bar_values"][0]["first"] != r["changed_bar_values"][0]["second"]


def test_nan_to_value_change_is_a_change_and_nan_equals_nan():
    a = day_bars(date(2026, 11, 10), last=2)
    nan_a = [a[0], RawBar(a[1].start_ts, NAN, NAN, NAN, NAN, 0.0)]
    assert compare_overlap(nan_a, list(nan_a))["changed_bars"] == 0
    assert compare_overlap(nan_a, a)["changed_bars"] == 1


# ---- price basis ------------------------------------------------------------------------------------


def daily(closes_by_day):
    return [RawBar(datetime(d.year, d.month, d.day, tzinfo=EXCHANGE_TZ), c, c + 1, c - 1, c, 1000, {"adj_close": c * 0.99}) for d, c in closes_by_day.items()]


def test_continuous_close_across_a_split_is_classified_split_adjusted():
    # RELIANCE 2024-10-28 bonus: a 2.0 ratio, yet prev close ~= ex close
    bars = daily({date(2024, 10, 25): 1327.85, date(2024, 10, 28): 1334.35})
    r = analyze_price_basis(bars, [(date(2024, 10, 28), 2.0)])
    assert r["classification"] == "CLOSE_APPEARS_SPLIT_ADJUSTED"
    assert r["split_tests"][0]["verdict"] == "SPLIT_ADJUSTED_LIKE"
    assert "UNVERIFIED_PROVIDER_NATIVE" in r["label_applied"]


def test_a_halving_across_a_split_looks_as_traded():
    bars = daily({date(2024, 10, 25): 2700.0, date(2024, 10, 28): 1350.0})
    r = analyze_price_basis(bars, [(date(2024, 10, 28), 2.0)])
    assert r["classification"] == "CLOSE_APPEARS_AS_TRADED_AT_SPLITS"


def test_no_split_in_range_is_inconclusive_and_never_verified():
    r = analyze_price_basis(daily({date(2024, 10, 25): 100.0, date(2024, 10, 28): 101.0}), [])
    assert r["classification"] == "INCONCLUSIVE_NO_SPLITS_IN_RANGE"
    assert "no verification claimed" in r["label_applied"]


def test_split_without_a_bar_on_the_ex_date_is_inconclusive():
    r = analyze_price_basis(daily({date(2024, 10, 25): 100.0}), [(date(2024, 10, 28), 2.0)])
    assert r["split_tests"][0]["verdict"] == "INCONCLUSIVE"


def test_adj_close_vs_close_differences_are_counted():
    r = analyze_price_basis(daily({date(2024, 10, 25): 100.0, date(2024, 10, 28): 101.0}), [])
    assert r["adj_close_present_rows"] == 2 and r["adj_close_differs_rows"] == 2


def test_daily_vs_intraday_only_compares_complete_sessions_and_ignores_nan():
    day = date(2026, 11, 10)
    intr = day_bars(day)
    intr[-1] = RawBar(intr[-1].start_ts, NAN, NAN, NAN, NAN, 0.0)
    d = [RawBar(datetime(2026, 11, 10, tzinfo=EXCHANGE_TZ), intr[0].open, max(b.high for b in intr[:-1]), min(b.low for b in intr[:-1]), intr[-2].close, sum(b.volume for b in intr[:-1]))]
    r = analyze_daily_vs_intraday(d, intr, "5m")
    assert r["complete_sessions_compared"] == 1 and r["sessions_with_invalid_rows"] == 1
    assert r["max_close_diff_pct"] == 0.0 and r["max_volume_diff_pct"] == 0.0
    partial = analyze_daily_vs_intraday(d, day_bars(day, last=30), "5m")
    assert partial == {"complete_sessions_compared": 0}


# ---- capability derivation ---------------------------------------------------------------------------


def symbol_report(ok=True, adj_equal=True, kinds=None, lag=-2.0):
    five = {
        "success": ok, "returned_candles": 2897, "first_bar_start_ist": "2026-09-15", "last_bar_lag_minutes": lag,
        "intraday_adj_close_differs_from_close": 0 if adj_equal else 5, "extras_present": ["adj_close"],
    }
    return {
        "symbol": "X", "provider_symbol": "X.NS", "accepted_lookback_days": 60,
        "five_minute": five if ok else {"success": False},
        "five_minute_attempts": [{"error_kind": k} for k in (kinds or [])] or [{"error_kind": None}],
        "daily": {"success": True}, "corporate_actions": {"success": True}, "quote": {"success": True}, "metadata": {"success": True},
        "price_basis": {"classification": "CLOSE_APPEARS_SPLIT_ADJUSTED", "split_tests": [{"verdict": "SPLIT_ADJUSTED_LIKE", "score": 0.0}]},
    }


def report(*syms):
    return {"probe_id": "p1", "target_days": 60, "symbols": list(syms)}


def test_full_success_verifies_capabilities_with_evidence():
    caps = derive_capabilities(report(symbol_report(), symbol_report()))
    for name in ("HISTORICAL_INTRADAY_CANDLES", "FIVE_MINUTE_INTERVAL", "LATEST_CANDLE", "HISTORICAL_DAILY_CANDLES", "CORPORATE_ACTIONS", "QUOTE_RETRIEVAL", "INSTRUMENT_METADATA"):
        assert caps[name]["status"] == "VERIFIED" and caps[name]["evidence"]["probe_id"] == "p1"
    assert caps["LATEST_CANDLE"]["evidence"]["in_progress_bar_returned"] is True
    assert "no market timestamp" in caps["QUOTE_RETRIEVAL"]["note"]


def test_identical_intraday_adj_close_means_adjusted_intraday_is_unsupported():
    caps = derive_capabilities(report(symbol_report(adj_equal=True)))
    assert caps["ADJUSTED_INTRADAY_PRICES"]["status"] == "UNSUPPORTED"
    differing = derive_capabilities(report(symbol_report(adj_equal=False)))
    assert differing["ADJUSTED_INTRADAY_PRICES"]["status"] == "UNKNOWN"


def test_one_failed_symbol_prevents_verification():
    caps = derive_capabilities(report(symbol_report(), symbol_report(ok=False, kinds=["RATE_LIMITED"])))
    assert "FIVE_MINUTE_INTERVAL" not in caps and "HISTORICAL_INTRADAY_CANDLES" not in caps


def test_transport_failures_never_downgrade_to_unsupported():
    caps = derive_capabilities(report(symbol_report(ok=False, kinds=["UNAVAILABLE"])))
    assert "FIVE_MINUTE_INTERVAL" not in caps


def test_definitive_no_data_answers_mark_unsupported():
    caps = derive_capabilities(report(symbol_report(ok=False, kinds=["NO_DATA", "INVALID_REQUEST"])))
    assert caps["FIVE_MINUTE_INTERVAL"]["status"] == "UNSUPPORTED"


def test_futures_capability_is_never_derived():
    assert "FUTURES_CONTRACT_DATA" not in derive_capabilities(report(symbol_report()))


def test_price_basis_observation_never_applies_a_verified_label():
    obs = derive_price_basis_observations(report(symbol_report()))
    assert obs["label_applied_to_stored_candles"] == "UNVERIFIED_PROVIDER_NATIVE"
    assert "NOT as-traded" in obs["conclusion"]
    assert derive_price_basis_observations(report({**symbol_report(), "price_basis": {"split_tests": []}}))["conclusion"].startswith("No split")


def test_reports_serialise_to_json_and_summarise():
    rep = {
        "probe_id": "p1", "yfinance_version": "1.8.0", "python": "3.14", "started_at": NOW,
        "symbols": [{"symbol": "X", "five_minute": {"success": False}, "five_minute_attempts": [{"lookback_days": 60, "error_kind": "NO_DATA"}]}],
    }
    json.dumps(jsonable({**rep, "nan": NAN, "when": NOW, "day": date(2026, 1, 1)}))
    assert "NO" in summarize(rep)
