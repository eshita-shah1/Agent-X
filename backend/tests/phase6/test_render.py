"""The rule-based explanation copies stored values and never states a number that is not in the facts (pure)."""

import json

import pytest

from app.explanations.render import OUTCOME_SENTENCES, render_decision, signal_sentence, as_text
from app.db.enums import AgentDecisionOutcome
from app.llm.narrator import ungrounded_numbers
from app.strategies.registry import STRATEGIES

VALUES = {
    "ORB_BREAKOUT": {"close": 102.25, "or_high": 101.5, "or_low": 99.75, "or_status": "COMPLETE"},
    "VWAP_CROSS_UP": {"close": 101.1, "vwap": 100.9, "prev_close": 100.7, "prev_vwap": 100.8, "atr": 0.42},
    "VWAP_CROSS_DOWN": {"close": 100.1, "vwap": 100.3, "prev_close": 100.5, "prev_vwap": 100.4, "atr": 0.42},
    "EMA_CROSS_UP": {"close": 101.0, "ema_fast": 100.61, "ema_slow": 100.58, "prev_ema_fast": 100.55,
                     "prev_ema_slow": 100.57, "atr": 0.3},
    "EMA_CROSS_DOWN": {"close": 99.0, "ema_fast": 99.41, "ema_slow": 99.44, "prev_ema_fast": 99.5, "prev_ema_slow": 99.47},
    "OVERSOLD_BELOW_LOWER_BAND": {"close": 98.2, "rsi": 24.7, "bb_lower": 98.4, "bb_mid": 99.9, "bb_upper": 101.4, "atr": 0.5},
    "REVERTED_TO_MIDDLE_BAND": {"close": 100.0, "rsi": 51.2, "bb_lower": 98.4, "bb_mid": 99.9, "bb_upper": 101.4, "atr": 0.5},
    "SUPERTREND_FLIP_UP": {"close": 101.3, "supertrend_line": 100.2, "direction": "UP", "flipped": True},
    "SUPERTREND_FLIP_DOWN": {"close": 99.3, "supertrend_line": 100.2, "direction": "DOWN", "flipped": True},
    "SESSION_SQUARE_OFF": {"close": 100.0},
}
STRATEGY = {"ORB_BREAKOUT": "ORB", "VWAP_CROSS_UP": "VWAP", "VWAP_CROSS_DOWN": "VWAP", "EMA_CROSS_UP": "EMA_CROSSOVER",
            "EMA_CROSS_DOWN": "EMA_CROSSOVER", "OVERSOLD_BELOW_LOWER_BAND": "RSI_BOLLINGER",
            "REVERTED_TO_MIDDLE_BAND": "RSI_BOLLINGER", "SUPERTREND_FLIP_UP": "SUPERTREND",
            "SUPERTREND_FLIP_DOWN": "SUPERTREND", "SESSION_SQUARE_OFF": "ORB"}


def params(strategy_id):
    from app.explanations.facts import plain

    return plain(STRATEGIES[strategy_id].defaults())


@pytest.mark.parametrize("reason", sorted(VALUES))
def test_every_signal_reason_is_explained_from_its_values_and_parameters(reason):
    p = params(STRATEGY[reason])
    sentence = signal_sentence(reason, VALUES[reason], p)
    assert "n/a" not in sentence, sentence  # every placeholder found its stored value
    assert ungrounded_numbers(sentence, json.dumps(VALUES[reason]) + json.dumps(p)) == [], sentence


def test_an_unknown_reason_lists_the_stored_values_verbatim():
    assert "foo 1.23456789" in signal_sentence("NEW_REASON", {"foo": 1.23456789}, {})


def test_every_agent_outcome_has_a_sentence():
    assert set(OUTCOME_SENTENCES) == {o.value for o in AgentDecisionOutcome}


def test_layers_stay_separate():
    facts = {"instrument": {"id": 1, "symbol": "INFY"}, "selection": None, "candle": None, "configuration": {},
             "strategy_signal": {"strategy_id": "VWAP", "strategy_version": "1", "config_id": "v", "action": "ENTER_LONG",
                                 "reason": "VWAP_CROSS_UP", "indicator_values": VALUES["VWAP_CROSS_UP"],
                                 "signal_bar_start_ist": "2026-01-05 10:00 IST"},
             "agent_decision": {"kind": "EVALUATION", "outcome": "SUPPRESSED_KILL_SWITCH", "detail": None},
             "risk_decision": None, "order": None, "execution": None}
    layers = [layer for layer, _ in render_decision(facts)]
    assert layers == ["Strategy signal", "Agent decision", "Outcome"]
    assert "kill switch" in as_text(render_decision(facts))


def test_signal_wording():
    base = {"instrument": {"id": 1, "symbol": "INFY"}, "selection": None, "candle": None, "configuration": {},
            "agent_decision": {"kind": "EVALUATION", "outcome": "NO_SIGNAL", "detail": None},
            "risk_decision": None, "order": None, "execution": None}
    signal = {"strategy_id": "ORB", "strategy_version": "1", "config_id": "o", "reason": "ORB_BREAKOUT",
              "indicator_values": VALUES["ORB_BREAKOUT"], "signal_bar_start_ist": "2026-01-05 09:30 IST"}
    entry = as_text(render_decision({**base, "strategy_signal": {**signal, "action": "ENTER_LONG"}}))
    assert "generated an entry signal (ORB_BREAKOUT)" in entry and " because the finalized bar" in entry
    none = as_text(render_decision({**base, "strategy_signal": {**signal, "action": "NONE", "reason": "COOLDOWN"}}))
    assert "gave no signal (COOLDOWN)" in none and "because" not in none
