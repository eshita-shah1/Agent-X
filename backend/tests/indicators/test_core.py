"""SMA, EMA, population standard deviation, Wilder smoothing, `run`, and input validation."""

from datetime import UTC, datetime, timedelta

import pytest

from app.indicators import EMA, SMA, IndicatorBar, RollingStd, WilderAverage, run
from app.indicators.core import BarSequence, check_number, check_period
from tests.indicators import DAY, F, V_CLOSES, assert_series, bar, close_enough, ist

# ---- reference vectors (dataset V closes, n = 3) ----------------------------------------------------------------


def test_sma3_reference_vector():
    assert_series(run(SMA(3), V_CLOSES), [None, None, 11, F(34, 3), 12, F(38, 3), F(40, 3), F(25, 2)])


def test_ema3_reference_vector_is_seeded_with_the_sma():
    assert_series(run(EMA(3), V_CLOSES), [None, None, 11, 11, 12, 13, 13, F(47, 4)])


def test_population_std3_reference_vector():
    variances = [None, None, F(2, 3), F(2, 9), F(2, 3), F(14, 9), F(2, 9), F(13, 6)]
    expected = [None if v is None else float(v) ** 0.5 for v in variances]
    assert_series(run(RollingStd(3), V_CLOSES), expected)


def test_wilder_average_seeds_with_the_mean_then_smooths():
    # seed mean(1, 2, 3) = 2; then (2 * 2 + 4) / 3 = 8/3; then (8/3 * 2 + 10) / 3 = 46/9
    assert_series(run(WilderAverage(3), [1, 2, 3, 4, 10]), [None, None, 2, F(8, 3), F(46, 9)])


# ---- edge cases -----------------------------------------------------------------------------------------------------


def test_period_one_is_the_identity_and_has_zero_dispersion():
    xs = [3.0, 1.5, 7.25]
    assert run(SMA(1), xs) == xs and run(EMA(1), xs) == xs and run(WilderAverage(1), xs) == xs
    assert run(RollingStd(1), xs) == [0.0, 0.0, 0.0]


def test_constant_series_has_zero_dispersion():
    assert run(RollingStd(4), [5.0] * 6) == [None, None, None, 0.0, 0.0, 0.0]
    assert run(SMA(4), [5.0] * 6)[3:] == [5.0] * 3 and run(EMA(4), [5.0] * 6)[3:] == [5.0] * 3


@pytest.mark.parametrize("n", [1, 2, 5, 9])
def test_warm_up_lengths(n):
    xs = [float(i + 1) for i in range(12)]
    for indicator in (SMA(n), EMA(n), RollingStd(n), WilderAverage(n)):
        out = run(indicator, xs)
        assert out[: n - 1] == [None] * (n - 1) and all(v is not None for v in out[n - 1 :])


def test_batch_run_equals_incremental_updates():
    a, b = EMA(3), EMA(3)
    assert run(a, V_CLOSES) == [b.update(x) for x in V_CLOSES]


# ---- validation -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, 2.5, True, False, "3", None])
def test_invalid_periods_are_rejected(bad):
    with pytest.raises(ValueError):
        check_period("period", bad)
    for cls in (SMA, EMA, RollingStd, WilderAverage):
        with pytest.raises(ValueError):
            cls(bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), True, "1", None])
def test_non_finite_or_non_numeric_values_are_rejected(bad):
    with pytest.raises(ValueError):
        check_number("value", bad)
    with pytest.raises(ValueError):
        SMA(2).update(bad)


def test_valid_bar_coerces_numbers_to_float():
    b = bar(DAY, 0, 10, 11, 9, 10, 0)
    assert (b.open, b.volume) == (10.0, 0.0) and all(isinstance(x, float) for x in (b.open, b.high, b.low, b.close, b.volume))


@pytest.mark.parametrize(
    "ohlcv",
    [
        (0, 11, 9, 10, 1),  # price <= 0
        (10, 11, -9, 10, 1),
        (float("nan"), 11, 9, 10, 1),
        (10, float("inf"), 9, 10, 1),
        (10, 11, 9, 12, 1),  # high < close
        (8, 11, 9, 10, 1),  # low > open
        (10, 11, 9, 10, -1),  # negative volume
        (10, 11, 9, 10, float("nan")),
        (True, 11, 9, 10, 1),
    ],
)
def test_invalid_bars_are_rejected(ohlcv):
    with pytest.raises(ValueError):
        bar(DAY, 0, *ohlcv)


def test_bar_timestamps_must_be_timezone_aware_and_match_the_session_date():
    with pytest.raises(ValueError, match="timezone-aware"):
        IndicatorBar(datetime(2026, 11, 2, 9, 15), DAY, 10, 11, 9, 10, 1)
    # 2026-11-02 23:00 UTC is already 2026-11-03 in IST
    with pytest.raises(ValueError, match="session_date"):
        IndicatorBar(datetime(2026, 11, 2, 23, 0, tzinfo=UTC), DAY, 10, 11, 9, 10, 1)
    assert IndicatorBar(ist(DAY, 9, 15).astimezone(UTC), DAY, 10, 11, 9, 10, 1).session_date == DAY  # UTC input is fine


def test_bar_sequence_requires_strictly_increasing_time_and_flags_new_sessions():
    seq = BarSequence()
    first, second = bar(DAY, 0, 10, 11, 9, 10), bar(DAY, 1, 10, 11, 9, 10)
    assert seq.accept(first) is True and seq.accept(second) is False
    with pytest.raises(ValueError, match="strictly increasing"):
        seq.accept(second)  # equal timestamp
    with pytest.raises(ValueError, match="strictly increasing"):
        seq.accept(first)  # going back in time
    nxt = IndicatorBar(ist(DAY, 9, 15) + timedelta(days=1), DAY + timedelta(days=1), 10, 11, 9, 10, 1)
    assert seq.accept(nxt) is True
    with pytest.raises(ValueError, match="IndicatorBar"):
        seq.accept((1, 2, 3))


def test_close_enough_is_tight():
    assert 1.0 + 1e-13 == close_enough(1) and 1.0 + 1e-9 != close_enough(1)
