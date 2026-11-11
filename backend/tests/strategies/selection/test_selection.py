"""Regime-gated priority selection and pinned mode, on hand-built histories (see the package docstring)."""

import dataclasses

import pytest

from app.strategies import EMACrossoverConfig, ORBConfig, SupertrendConfig
from app.strategies.selection import (
    Candidate,
    CandidateOutcome,
    InstrumentSelector,
    PinnedPolicy,
    Regime,
    RegimePriorityPolicy,
    SelectionMode,
    SelectionReason,
    SelectionStatus,
)
from tests.strategies.selection import ALL, EMA, NEXT_DAY, ORB, RSI_BB, SUPERTREND, VWAP, flat, mixed, trending

O = CandidateOutcome


def decide(policy, bars):
    sel = InstrumentSelector("RELIANCE", policy)
    for b in bars:
        sel.observe(b)
    return sel.decision(NEXT_DAY)


def outcomes(decision):
    return {c.config_id: c.outcome for c in decision.candidates}


# ---- regime-gated priority -----------------------------------------------------------------------------------------------


def test_trend_selects_the_highest_priority_trend_or_opening_breakout_candidate():
    d = decide(RegimePriorityPolicy(ALL), trending())
    assert (d.regime.regime, d.status, d.reason) == (Regime.TREND, SelectionStatus.SELECTED, SelectionReason.SELECTED_HIGHEST_PRIORITY_ELIGIBLE)
    assert d.selected_config_id == "orb-1" and d.selected.family == "OPENING_BREAKOUT"
    assert outcomes(d) == {
        "orb-1": O.SELECTED, "ema-1": O.NOT_SELECTED_LOWER_PRIORITY, "st-1": O.NOT_SELECTED_LOWER_PRIORITY,
        "rsi-1": O.REJECTED_REGIME_INELIGIBLE, "vwap-1": O.NOT_SELECTED_LOWER_PRIORITY,
    }


def test_range_allows_only_mean_reversion():
    d = decide(RegimePriorityPolicy(ALL), flat())
    assert d.regime.regime is Regime.RANGE and d.selected_config_id == "rsi-1"
    assert {k for k, v in outcomes(d).items() if v is O.REJECTED_REGIME_INELIGIBLE} == {"orb-1", "ema-1", "st-1", "vwap-1"}


def test_undetermined_selects_nothing():
    d = decide(RegimePriorityPolicy(ALL), mixed())
    assert (d.regime.regime, d.status, d.reason, d.selected) == (Regime.UNDETERMINED, SelectionStatus.NO_SELECTION, SelectionReason.NO_SELECTION_UNDETERMINED_REGIME, None)
    assert set(outcomes(d).values()) == {O.REJECTED_REGIME_INELIGIBLE}


def test_priority_is_explicit_not_list_order():
    reordered = tuple(reversed(ALL))
    assert decide(RegimePriorityPolicy(reordered), trending()).selected_config_id == "orb-1"
    swapped = (dataclasses.replace(ORB, priority=2), dataclasses.replace(EMA, priority=1))
    assert decide(RegimePriorityPolicy(swapped), trending()).selected_config_id == "ema-1"
    assert [c.priority for c in decide(RegimePriorityPolicy(reordered), trending()).candidates] == [1, 2, 3, 4, 5]  # evaluated in priority order


def test_an_incomplete_opening_range_makes_orb_warmup_ineligible():
    d = decide(RegimePriorityPolicy(ALL), trending(day2_slots=(0, 2)))  # 09:20 bar missing
    assert d.regime.regime is Regime.TREND and d.selected_config_id == "ema-1"
    orb = d.candidates[0]
    assert orb.outcome is O.REJECTED_WARMUP_NOT_SATISFIED and orb.warmup_satisfied is False and "INCOMPLETE" in orb.warmup_requirement


def test_a_longer_opening_range_cannot_be_complete_at_the_09_30_checkpoint():
    orb20 = Candidate("orb-20", "ORB", ORBConfig(range_minutes=20), 1)
    d = decide(RegimePriorityPolicy((orb20, EMA)), trending())
    assert outcomes(d) == {"orb-20": O.REJECTED_WARMUP_NOT_SATISFIED, "ema-1": O.SELECTED}


def test_bar_count_warmup_ineligible_candidates_are_rejected():
    slow = Candidate("ema-slow", "EMA_CROSSOVER", EMACrossoverConfig(fast_period=9, slow_period=100), 1)  # needs 101 bars
    d = decide(RegimePriorityPolicy((slow, SUPERTREND)), trending())  # 78 bars available
    assert outcomes(d) == {"ema-slow": O.REJECTED_WARMUP_NOT_SATISFIED, "st-1": O.SELECTED}
    assert d.candidates[0].warmup_requirement == "101 bars; 78 available"


def test_no_selection_when_every_candidate_is_ineligible():
    slow = Candidate("ema-slow", "EMA_CROSSOVER", EMACrossoverConfig(slow_period=100), 1)
    d = decide(RegimePriorityPolicy((slow, RSI_BB)), trending())
    assert (d.status, d.reason) == (SelectionStatus.NO_SELECTION, SelectionReason.NO_SELECTION_NO_ELIGIBLE_CANDIDATE)
    assert outcomes(d) == {"ema-slow": O.REJECTED_WARMUP_NOT_SATISFIED, "rsi-1": O.REJECTED_REGIME_INELIGIBLE}


def test_invalid_candidates_are_reported_and_skipped():
    unknown = Candidate("macd-1", "MACD", EMACrossoverConfig(), 1)
    mismatched = Candidate("ema-bad", "EMA_CROSSOVER", SupertrendConfig(), 2)
    d = decide(RegimePriorityPolicy((unknown, mismatched, VWAP)), trending())
    assert outcomes(d) == {"macd-1": O.REJECTED_INVALID_CONFIGURATION, "ema-bad": O.REJECTED_INVALID_CONFIGURATION, "vwap-1": O.SELECTED}
    assert "unknown strategy" in d.candidates[0].detail and "needs a EMACrossoverConfig" in d.candidates[1].detail


@pytest.mark.parametrize(
    "candidates",
    [(), [ORB], (ORB, dataclasses.replace(EMA, config_id="orb-1")), (ORB, dataclasses.replace(EMA, priority=1)), (Candidate("x", "ORB", ORBConfig()),), ("ORB",)],
)
def test_structurally_invalid_policies_are_rejected(candidates):
    with pytest.raises(ValueError):
        RegimePriorityPolicy(candidates)


@pytest.mark.parametrize("kwargs", [{"config_id": ""}, {"config_id": 5}, {"priority": 0}, {"priority": True}, {"priority": 1.0}, {"strategy_id": None}])
def test_invalid_candidate_fields(kwargs):
    base = {"config_id": "c", "strategy_id": "ORB", "config": ORBConfig(), "priority": 1}
    with pytest.raises(ValueError):
        Candidate(**{**base, **kwargs})


def test_candidates_carry_no_performance_information():
    assert {f.name for f in dataclasses.fields(Candidate)} == {"config_id", "strategy_id", "config", "priority"}


# ---- pinned ------------------------------------------------------------------------------------------------------------------


def test_a_valid_pinned_configuration_is_selected_whatever_the_regime():
    for bars, regime in ((trending(), Regime.TREND), (flat(), Regime.RANGE), (mixed(), Regime.UNDETERMINED)):
        d = decide(PinnedPolicy(EMA), bars)
        assert (d.mode, d.status, d.reason, d.selected_config_id, d.regime.regime) == (
            SelectionMode.PINNED, SelectionStatus.SELECTED, SelectionReason.SELECTED_PINNED, "ema-1", regime,
        )
        assert d.selected.regime_eligible is (regime is Regime.TREND)  # recorded, not enforced


def test_pinned_warmup_is_recorded_but_not_enforced():
    slow = Candidate("ema-slow", "EMA_CROSSOVER", EMACrossoverConfig(slow_period=100), None)
    d = decide(PinnedPolicy(slow), trending())
    assert d.status is SelectionStatus.SELECTED and d.selected.warmup_satisfied is False


@pytest.mark.parametrize("candidate", [Candidate("macd", "MACD", EMACrossoverConfig()), Candidate("ema-bad", "EMA_CROSSOVER", ORBConfig())])
def test_an_invalid_pinned_configuration_never_falls_back(candidate):
    sel = InstrumentSelector("RELIANCE", PinnedPolicy(candidate))
    for b in trending():
        sel.observe(b)
    d = sel.decision(NEXT_DAY)
    assert (d.status, d.reason, d.selected) == (SelectionStatus.NO_SELECTION, SelectionReason.NO_SELECTION_PINNED_CONFIGURATION_INVALID, None)
    assert [c.outcome for c in d.candidates] == [O.REJECTED_INVALID_CONFIGURATION]
    assert not any(sel.entry_permitted(c.config_id, NEXT_DAY) for c in ALL + (candidate,))


def test_pinned_policy_validation():
    with pytest.raises(ValueError):
        PinnedPolicy("ema-1")
