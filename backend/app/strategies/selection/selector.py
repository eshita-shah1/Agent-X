"""The selection decision itself: pure functions of (policy, regime result, warm-up context).

Regime-priority mode evaluates candidates in priority order: configuration validity -> regime eligibility ->
warm-up -> the first one passing all three is SELECTED, later passing ones are NOT_SELECTED_LOWER_PRIORITY.
Pinned mode selects its single configuration if it is valid, whatever the regime or warm-up (both are recorded),
and otherwise returns NO_SELECTION: there is no fallback.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace

from app.indicators.core import IndicatorBar
from app.indicators.session import OpeningRange, OpeningRangeStatus
from app.marketdata.calendar import TradingCalendar
from app.strategies.registry import STRATEGIES
from app.strategies.selection.candidates import (
    ELIGIBLE_FAMILIES,
    Candidate,
    CandidateEvaluation,
    CandidateOutcome,
    configuration_problem,
    required_bars,
)
from app.strategies.selection.policy import PinnedPolicy, Policy, RegimePriorityPolicy
from app.strategies.selection.regime import Regime, RegimeResult


class SelectionStatus(str, enum.Enum):
    SELECTED = "SELECTED"
    NO_SELECTION = "NO_SELECTION"


class SelectionReason(str, enum.Enum):
    SELECTED_HIGHEST_PRIORITY_ELIGIBLE = "SELECTED_HIGHEST_PRIORITY_ELIGIBLE"
    SELECTED_PINNED = "SELECTED_PINNED"
    NO_SELECTION_UNDETERMINED_REGIME = "NO_SELECTION_UNDETERMINED_REGIME"
    NO_SELECTION_NO_ELIGIBLE_CANDIDATE = "NO_SELECTION_NO_ELIGIBLE_CANDIDATE"
    NO_SELECTION_PINNED_CONFIGURATION_INVALID = "NO_SELECTION_PINNED_CONFIGURATION_INVALID"


@dataclass(frozen=True)
class WarmupContext:
    """What was available at the checkpoint: the bar count across sessions and this session's bars."""

    bars_available: int
    session_bars: tuple[IndicatorBar, ...]
    calendar: TradingCalendar


@dataclass(frozen=True)
class SelectionOutcome:
    status: SelectionStatus
    reason: SelectionReason
    selected: CandidateEvaluation | None
    candidates: tuple[CandidateEvaluation, ...]


def _warmup(candidate: Candidate, context: WarmupContext) -> tuple[str, bool]:
    if candidate.strategy_id == "ORB":
        minutes = candidate.config.range_minutes
        status = OpeningRangeStatus.FORMING
        indicator = OpeningRange(minutes, context.calendar)
        for bar in context.session_bars:
            status = indicator.update(bar).status
        return f"opening range ({minutes} min) COMPLETE by the checkpoint; status {status.value}", status is OpeningRangeStatus.COMPLETE
    needed = required_bars(candidate.strategy_id, candidate.config)
    return f"{needed} bars; {context.bars_available} available", context.bars_available >= needed


def _evaluate(candidate: Candidate, regime: Regime, context: WarmupContext, enforce: bool = True) -> CandidateEvaluation:
    """Assess one candidate. With enforce=False (pinned mode) regime eligibility and warm-up are recorded only."""
    problem = configuration_problem(candidate)
    if problem is not None:
        return CandidateEvaluation(
            candidate.config_id, candidate.strategy_id, None, None, candidate.priority, False, None, None, None,
            CandidateOutcome.REJECTED_INVALID_CONFIGURATION, problem,
        )
    definition = STRATEGIES[candidate.strategy_id]
    requirement, warm = _warmup(candidate, context)
    eligible = definition.family in ELIGIBLE_FAMILIES[regime]
    base = (candidate.config_id, candidate.strategy_id, definition.version, definition.family.value, candidate.priority, True, eligible, requirement, warm)
    if enforce and not eligible:
        return CandidateEvaluation(*base, CandidateOutcome.REJECTED_REGIME_INELIGIBLE, f"family {definition.family.value} is not eligible in a {regime.value} regime")
    if enforce and not warm:
        return CandidateEvaluation(*base, CandidateOutcome.REJECTED_WARMUP_NOT_SATISFIED, f"warm-up not satisfied: {requirement}")
    return CandidateEvaluation(*base, CandidateOutcome.SELECTED, "eligible")


def select(policy: Policy, regime: RegimeResult, context: WarmupContext) -> SelectionOutcome:
    if isinstance(policy, PinnedPolicy):
        evaluation = _evaluate(policy.candidate, regime.regime, context, enforce=False)
        if evaluation.outcome is CandidateOutcome.REJECTED_INVALID_CONFIGURATION:
            return SelectionOutcome(SelectionStatus.NO_SELECTION, SelectionReason.NO_SELECTION_PINNED_CONFIGURATION_INVALID, None, (evaluation,))
        evaluation = replace(evaluation, detail="pinned by the user; regime and warm-up recorded, not required")
        return SelectionOutcome(SelectionStatus.SELECTED, SelectionReason.SELECTED_PINNED, evaluation, (evaluation,))

    if not isinstance(policy, RegimePriorityPolicy):
        raise ValueError(f"unknown policy type {type(policy).__name__}")
    evaluations, selected = [], None
    for candidate in policy.ordered():
        evaluation = _evaluate(candidate, regime.regime, context)
        if evaluation.outcome is CandidateOutcome.SELECTED:
            if selected is None:
                selected = evaluation = replace(evaluation, detail="highest-priority candidate passing validity, regime and warm-up")
            else:
                evaluation = replace(
                    evaluation, outcome=CandidateOutcome.NOT_SELECTED_LOWER_PRIORITY,
                    detail=f"eligible, but priority {selected.priority} ({selected.config_id}) is higher",
                )
        evaluations.append(evaluation)
    if selected is not None:
        return SelectionOutcome(SelectionStatus.SELECTED, SelectionReason.SELECTED_HIGHEST_PRIORITY_ELIGIBLE, selected, tuple(evaluations))
    reason = SelectionReason.NO_SELECTION_UNDETERMINED_REGIME if regime.regime is Regime.UNDETERMINED else SelectionReason.NO_SELECTION_NO_ELIGIBLE_CANDIDATE
    return SelectionOutcome(SelectionStatus.NO_SELECTION, reason, None, tuple(evaluations))
