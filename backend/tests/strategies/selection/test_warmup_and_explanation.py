"""Warm-up requirements agree with the actual strategies, and every decision has a complete, JSON-ready explanation."""

import json

import pytest

from app.strategies import (
    FLAT,
    STRATEGIES,
    EMACrossoverConfig,
    Reason,
    RSIBollingerConfig,
    SupertrendConfig,
    VWAPConfig,
)
from app.strategies.selection import InstrumentSelector, RegimePriorityPolicy, required_bars
from tests.strategies import DAY, NEXT_DAY, pbar
from tests.strategies.selection import ALL, trending

READY_KEYS = {
    "VWAP": ("atr",),
    "EMA_CROSSOVER": ("ema_fast", "ema_slow", "prev_ema_fast", "prev_ema_slow", "atr"),
    "RSI_BOLLINGER": ("rsi", "bb_lower", "atr"),
    "SUPERTREND": ("direction",),
}
CONFIGS = [
    ("VWAP", VWAPConfig()), ("VWAP", VWAPConfig(atr_period=3)),
    ("EMA_CROSSOVER", EMACrossoverConfig()), ("EMA_CROSSOVER", EMACrossoverConfig(fast_period=2, slow_period=3, atr_period=5)),
    ("RSI_BOLLINGER", RSIBollingerConfig()), ("RSI_BOLLINGER", RSIBollingerConfig(rsi_period=25, bollinger_period=10)),
    ("SUPERTREND", SupertrendConfig()), ("SUPERTREND", SupertrendConfig(atr_period=4)),
]


@pytest.mark.parametrize("strategy_id,config", CONFIGS, ids=[f"{s}-{i}" for i, (s, _) in enumerate(CONFIGS)])
def test_required_bars_is_exactly_when_the_strategy_stops_reporting_not_ready(strategy_id, config):
    strategy = STRATEGIES[strategy_id].create(config)
    bars = [pbar(DAY, i, 100.0 + (i % 7) * 0.4) for i in range(75)]
    signals = [strategy.evaluate(b, FLAT) for b in bars]
    ready = [all(s.values[k] is not None for k in READY_KEYS[strategy_id]) for s in signals]
    n = required_bars(strategy_id, config)
    assert ready.index(True) == n - 1 and all(ready[n - 1 :])
    # and the strategy itself agrees whenever no earlier gate hides the warm-up
    assert all(s.reason is Reason.INDICATOR_NOT_READY for s in signals[: n - 1] if s.reason not in (Reason.BEFORE_ELIGIBLE_TIME,))


def test_approved_default_requirements():
    assert {s: required_bars(s, STRATEGIES[s].default_config()) for s in READY_KEYS} == {
        "VWAP": 15, "EMA_CROSSOVER": 22, "RSI_BOLLINGER": 20, "SUPERTREND": 11,
    }
    assert required_bars("ORB", STRATEGIES["ORB"].default_config()) is None
    with pytest.raises(ValueError):
        required_bars("MACD", EMACrossoverConfig())


def decision():
    sel = InstrumentSelector("RELIANCE", RegimePriorityPolicy(ALL))
    for b in trending():
        sel.observe(b)
    return sel.decision(NEXT_DAY)


def test_the_explanation_contains_every_required_element():
    d = decision().as_dict()
    assert d["instrument_id"] == "RELIANCE" and d["session_date"] == "2026-11-03" and d["mode"] == "REGIME_PRIORITY"
    assert d["checkpoint"] == "2026-11-03T09:30:00+05:30" and d["locked"] is True
    assert d["status"] == "SELECTED" and d["reason"] == "SELECTED_HIGHEST_PRIORITY_ELIGIBLE"
    regime = d["regime"]
    assert (regime["regime"], regime["reason"], regime["adx"], regime["plus_di"], regime["minus_di"]) == (
        "TREND", "ADX_AT_OR_ABOVE_TREND_THRESHOLD", 100.0, 50.0, 0.0,
    )
    assert regime["input_bar_start"] == "2026-11-03T09:25:00+05:30" and regime["bars_used"] == 78
    assert d["selected"]["config_id"] == "orb-1"
    expected_keys = {
        "config_id", "strategy_id", "strategy_version", "family", "priority", "configuration_valid", "regime_eligible",
        "warmup_requirement", "warmup_satisfied", "outcome", "detail",
    }
    assert all(set(c) == expected_keys for c in d["candidates"])
    rsi = next(c for c in d["candidates"] if c["config_id"] == "rsi-1")
    assert (rsi["strategy_version"], rsi["family"], rsi["regime_eligible"], rsi["outcome"]) == ("1", "MEAN_REVERSION", False, "REJECTED_REGIME_INELIGIBLE")
    assert "not eligible in a TREND regime" in rsi["detail"]
    ema = next(c for c in d["candidates"] if c["config_id"] == "ema-1")
    assert ema["warmup_requirement"] == "22 bars; 78 available" and ema["warmup_satisfied"] is True
    assert "priority 1 (orb-1) is higher" in ema["detail"]


def test_the_explanation_is_json_serialisable_and_deterministic():
    a, b = decision().as_dict(), decision().as_dict()
    assert a == b and json.loads(json.dumps(a)) == a
    assert type(a["regime"]["regime"]) is str and type(a["candidates"][0]["outcome"]) is str  # plain values, not enums
