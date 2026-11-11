"""The strategy catalogue and the approved initial defaults (decision D7)."""

from dataclasses import FrozenInstanceError
from datetime import time

import pytest

from app.strategies import DEFAULTS_NOTE, DEFAULTS_STATUS, STRATEGIES, Family, get_definition

COMMON = {"square_off": time(15, 15), "cooldown_bars": 1}
APPROVED = {
    "ORB": (Family.OPENING_BREAKOUT, {**COMMON, "entry_cutoff": time(11, 30), "range_minutes": 15, "target_multiple": 1.5}),
    "VWAP": (Family.TREND, {**COMMON, "entry_cutoff": time(14, 45), "eligible_from": time(9, 30), "atr_period": 14, "stop_atr_multiple": 1.5}),
    "EMA_CROSSOVER": (
        Family.TREND,
        {**COMMON, "entry_cutoff": time(14, 45), "fast_period": 9, "slow_period": 21, "atr_period": 14, "stop_atr_multiple": 2.0},
    ),
    "RSI_BOLLINGER": (
        Family.MEAN_REVERSION,
        {
            **COMMON, "entry_cutoff": time(14, 45), "rsi_period": 14, "oversold": 30.0, "overbought": 70.0,
            "bollinger_period": 20, "bollinger_k": 2.0, "atr_period": 14, "stop_atr_multiple": 1.5,
        },
    ),
    "SUPERTREND": (Family.TREND, {**COMMON, "entry_cutoff": time(14, 45), "atr_period": 10, "multiplier": 3.0}),
}


def test_exactly_the_five_approved_strategies_with_stable_ids_and_version_1():
    assert set(STRATEGIES) == set(APPROVED)
    assert all(d.version == "1" and d.strategy_id == key for key, d in STRATEGIES.items())


@pytest.mark.parametrize("strategy_id", sorted(APPROVED))
def test_defaults_are_exactly_the_approved_initial_values(strategy_id):
    family, defaults = APPROVED[strategy_id]
    d = get_definition(strategy_id)
    assert d.family is family and d.defaults() == defaults


def test_defaults_are_labelled_approved_initial_not_optimized():
    assert DEFAULTS_STATUS == "APPROVED_INITIAL_DEFAULTS"
    assert "not empirically optimized" in DEFAULTS_NOTE and "not evidence of profitability" in DEFAULTS_NOTE


@pytest.mark.parametrize("strategy_id", sorted(APPROVED))
def test_each_definition_documents_its_entry_and_exit_rules(strategy_id):
    text = get_definition(strategy_id).description
    assert "Entry" in text and "Exit" in text and "long-only" in text


@pytest.mark.parametrize("strategy_id", sorted(APPROVED))
def test_create_returns_a_fresh_independent_instance(strategy_id):
    d = get_definition(strategy_id)
    a, b = d.create(), d.create()
    assert a is not b and a.config == b.config == d.default_config()


def test_configs_are_immutable_and_strategy_specific():
    cfg = get_definition("EMA_CROSSOVER").default_config()
    with pytest.raises(FrozenInstanceError):
        cfg.fast_period = 5
    with pytest.raises(ValueError, match="needs a"):
        get_definition("VWAP").create(cfg)


def test_unknown_strategy_is_rejected():
    with pytest.raises(ValueError, match="unknown strategy"):
        get_definition("MACD")


def test_the_catalogue_is_read_only():
    with pytest.raises(TypeError):
        STRATEGIES["X"] = None
