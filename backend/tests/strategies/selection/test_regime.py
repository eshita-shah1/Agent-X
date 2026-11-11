"""Regime classification: exact thresholds, unavailable and invalid values, the approved ADX indicator."""

from datetime import UTC, time

import pytest

from app.indicators import ADX, ADXValue, run
from app.strategies import EMACrossoverConfig
from app.strategies.selection import (
    Candidate,
    InstrumentSelector,
    PinnedPolicy,
    Regime,
    RegimeConfig,
    RegimeReason,
    RegimeTracker,
    classify,
)
from tests.strategies.selection import NEXT_DAY, flat, mixed, trending

CFG = RegimeConfig()
T, R, U = Regime.TREND, Regime.RANGE, Regime.UNDETERMINED


def adx(value, plus=30.0, minus=10.0):
    return ADXValue(plus, minus, 50.0, value)


@pytest.mark.parametrize(
    "value,regime,reason",
    [
        (25.0, T, RegimeReason.ADX_AT_OR_ABOVE_TREND_THRESHOLD),  # exactly 25 is TREND
        (25.000001, T, RegimeReason.ADX_AT_OR_ABOVE_TREND_THRESHOLD),
        (100.0, T, RegimeReason.ADX_AT_OR_ABOVE_TREND_THRESHOLD),
        (24.999999, U, RegimeReason.ADX_BETWEEN_THRESHOLDS),
        (22.5, U, RegimeReason.ADX_BETWEEN_THRESHOLDS),
        (20.000001, U, RegimeReason.ADX_BETWEEN_THRESHOLDS),
        (20.0, R, RegimeReason.ADX_AT_OR_BELOW_RANGE_THRESHOLD),  # exactly 20 is RANGE
        (0.0, R, RegimeReason.ADX_AT_OR_BELOW_RANGE_THRESHOLD),
    ],
)
def test_threshold_boundaries(value, regime, reason):
    assert classify(adx(value), CFG) == (regime, reason)


@pytest.mark.parametrize("value", [None, ADXValue(30.0, 10.0, 50.0, None)])
def test_missing_adx_is_undetermined_not_ready(value):
    assert classify(value, CFG) == (U, RegimeReason.ADX_NOT_READY)


@pytest.mark.parametrize(
    "value",
    [adx(float("nan")), adx(float("inf")), adx(-1.0), adx(100.5), adx(True), adx(30.0, plus=float("nan")), adx(30.0, minus=-5.0)],
)
def test_invalid_indicator_values_are_undetermined(value):
    assert classify(value, CFG) == (U, RegimeReason.INVALID_INDICATOR_VALUE)


def test_classification_is_deterministic():
    assert classify(adx(23.0), CFG) == classify(adx(23.0), CFG)


@pytest.mark.parametrize(
    "kwargs",
    [{"trend_min": 20.0}, {"range_max": 25.0}, {"trend_min": 101.0}, {"range_max": -1.0}, {"adx_period": 0}, {"checkpoint": time(9, 32)}, {"checkpoint": time(8, 0)}],
)
def test_invalid_regime_configuration(kwargs):
    with pytest.raises(ValueError):
        RegimeConfig(**kwargs)


def test_approved_defaults():
    assert (CFG.adx_period, CFG.trend_min, CFG.range_max, CFG.checkpoint) == (14, 25.0, 20.0, time(9, 30))


def checkpoint_regime(bars):
    sel = InstrumentSelector("X", PinnedPolicy(Candidate("p", "EMA_CROSSOVER", EMACrossoverConfig())))
    for b in bars:
        sel.observe(b)
    return sel.decision(NEXT_DAY).regime


def test_end_to_end_regimes_from_hand_built_histories():
    trend, rng, between = checkpoint_regime(trending()), checkpoint_regime(flat()), checkpoint_regime(mixed())
    assert (trend.regime, trend.adx, trend.plus_di, trend.minus_di) == (T, 100.0, 50.0, 0.0)
    assert (rng.regime, rng.adx, rng.plus_di, rng.minus_di) == (R, 0.0, 0.0, 0.0)
    assert between.regime is U and between.reason is RegimeReason.ADX_BETWEEN_THRESHOLDS
    assert between.adx == pytest.approx(22.540793846762597)


def test_the_regime_uses_the_approved_adx_indicator():
    bars = mixed()
    assert checkpoint_regime(bars).adx == run(ADX(14), bars)[-1].adx


def test_warm_up_not_satisfied_at_the_checkpoint_is_undetermined():
    short = trending()[-20:]  # only 20 bars: ADX(14) needs 28
    r = checkpoint_regime(short)
    assert (r.regime, r.reason, r.adx, r.bars_used) == (U, RegimeReason.ADX_NOT_READY, None, 20)


def test_result_records_checkpoint_and_input_bar():
    r = checkpoint_regime(trending())
    assert r.checkpoint.astimezone(UTC).isoformat() == "2026-11-03T04:00:00+00:00"  # 09:30 IST
    assert r.input_bar_start.astimezone(UTC).isoformat() == "2026-11-03T03:55:00+00:00"  # the 09:25 bar
    assert r.input_bar_end == r.checkpoint and (r.bars_used, r.session_bars_used) == (78, 3)
    assert (r.adx_period, r.trend_min, r.range_max) == (14, 25.0, 20.0)


def test_the_tracker_refuses_a_snapshot_after_a_bar_past_the_checkpoint():
    tracker = RegimeTracker(CFG)
    bars = trending(day2_slots=range(4))  # includes the 09:30 bar (ends 09:35)
    for b in bars:
        tracker.update(b)
    with pytest.raises(ValueError, match="future"):
        tracker.snapshot(checkpoint_regime(trending()).checkpoint, 4)
